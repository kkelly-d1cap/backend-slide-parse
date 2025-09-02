import os
import tempfile
import uuid
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename
from pdf2image import convert_from_path
import base64
from io import BytesIO
from PIL import Image
import boto3
from botocore.exceptions import ClientError

app = Flask(__name__)
CORS(app)

# Configuration
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size
ALLOWED_EXTENSIONS = {'pdf'}

# AWS S3 Configuration
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
S3_BUCKET_NAME = os.getenv('S3_BUCKET_NAME')

# In-memory session storage (use Redis/database in production)
session_storage = {}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_s3_client():
    """Initialize S3 client"""
    try:
        return boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION
        )
    except Exception as e:
        app.logger.error(f"Failed to initialize S3 client: {str(e)}")
        return None

def sanitize_filename(text):
    """Sanitize text for use in filenames"""
    import re
    sanitized = re.sub(r'[^\w\s-]', '', text)
    sanitized = re.sub(r'[-\s]+', '_', sanitized)
    return sanitized.strip('_')

def convert_pdf_to_images(pdf_path):
    """Convert PDF to images and return slide data with stored images"""
    try:
        images = convert_from_path(pdf_path, dpi=150, fmt='PNG')
        
        slides = []
        temp_images = []
        
        for i, image in enumerate(images):
            # Create thumbnail for preview
            thumbnail = image.copy()
            thumbnail.thumbnail((300, 200), Image.Resampling.LANCZOS)
            
            # Convert thumbnail to base64
            buffer = BytesIO()
            thumbnail.save(buffer, format='PNG')
            thumbnail_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            # Store full-size image in memory for later S3 upload
            full_buffer = BytesIO()
            image.save(full_buffer, format='PNG')
            full_image_data = full_buffer.getvalue()
            
            slide_data = {
                'id': i + 1,
                'thumbnail': f'data:image/png;base64,{thumbnail_b64}',
                'title': f'Slide {i + 1}',
                'selected': False,
                'category': None
            }
            
            slides.append(slide_data)
            temp_images.append(full_image_data)
        
        return slides, temp_images
    except Exception as e:
        app.logger.error(f"Error converting PDF: {str(e)}")
        return [], []

@app.route('/')
def home():
    """Root endpoint"""
    return jsonify({
        "message": "Slide Parser API is running!",
        "version": "1.0.0",
        "status": "healthy",
        "platform": "heroku",
        "endpoints": [
            "/health - Health check",
            "/api/test - API status", 
            "/api/upload - Upload PDF files",
            "/api/process - Upload slides to S3 and generate HTML"
        ]
    })

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "platform": "heroku",
        "server": "gunicorn"
    })

@app.route('/api/test')
def test():
    """Test endpoint to verify API and AWS configuration"""
    aws_configured = bool(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and S3_BUCKET_NAME)
    
    return jsonify({
        'message': 'Slide Parser API is working!',
        'platform': 'heroku',
        'environment': os.getenv('FLASK_ENV', 'production'),
        'aws_configured': aws_configured,
        'aws_region': AWS_REGION,
        's3_bucket': S3_BUCKET_NAME if aws_configured else 'Not configured',
        'endpoints': {
            'upload': '/api/upload - Upload and process PDF',
            'process': '/api/process - Upload slides to S3 and generate HTML',
            'test': '/api/test - This endpoint'
        }
    })

@app.route('/api/upload', methods=['POST'])
def upload_pdf():
    """Upload and process PDF file"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        fund_id = request.form.get('fund_id', '').strip()
        fund_name = request.form.get('fund_name', '').strip()
        
        if not fund_id or not fund_name:
            return jsonify({'error': 'Fund ID and Fund Name are required'}), 400
        
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'error': 'Invalid file type. Only PDF files are allowed.'}), 400
        
        # Check file size
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)
        
        if file_size > app.config['MAX_CONTENT_LENGTH']:
            return jsonify({'error': 'File size exceeds 50MB limit'}), 400
        
        # Create temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
            file.save(temp_file.name)
            
            # Process PDF and get slide data + images
            slides, temp_images = convert_pdf_to_images(temp_file.name)
            
            # Clean up temp PDF file
            os.unlink(temp_file.name)
        
        if not slides:
            return jsonify({'error': 'Failed to process PDF'}), 500
        
        # Generate session ID and store data
        session_id = str(uuid.uuid4())
        
        # Store session data including images for later S3 upload
        session_storage[session_id] = {
            'slides': slides,
            'temp_images': temp_images,
            'fund_id': fund_id,
            'fund_name': fund_name,
            'safe_fund_id': sanitize_filename(fund_id),
            'safe_fund_name': sanitize_filename(fund_name)
        }
        
        app.logger.info(f"PDF processed successfully. Session: {session_id}, Slides: {len(slides)}")
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'slides': slides,
            'fund_id': fund_id,
            'fund_name': fund_name,
            'total_slides': len(slides)
        })
    
    except Exception as e:
        app.logger.error(f"Error processing upload: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/process', methods=['POST'])
def process_slides():
    """Upload selected slides to S3 and generate HTML with real URLs"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        selected_slides = data.get('selected_slides', [])
        
        app.logger.info(f"Processing slides for session: {session_id}")
        
        if not session_id or not selected_slides:
            return jsonify({'error': 'Missing session ID or selected slides'}), 400
        
        if session_id not in session_storage:
            return jsonify({'error': 'Session not found or expired'}), 404
        
        session_data = session_storage[session_id]
        
        # Check AWS configuration
        if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET_NAME]):
            app.logger.error("AWS not configured properly")
            return jsonify({'error': 'AWS S3 not configured'}), 500
        
        # Initialize S3 client
        s3_client = get_s3_client()
        if not s3_client:
            return jsonify({'error': 'Failed to connect to S3'}), 500
        
        app.logger.info(f"S3 client initialized. Uploading {len(selected_slides)} slides...")
        
        # Process selected slides
        uploaded_slides = {}
        
        for selected_slide in selected_slides:
            slide_id = selected_slide.get('id')
            category = selected_slide.get('category')
            
            if not slide_id or not category:
                continue
            
            # Get the image data for this slide
            image_index = slide_id - 1  # Convert to 0-based index
            if image_index >= len(session_data['temp_images']):
                continue
            
            image_data = session_data['temp_images'][image_index]
            
            # Generate S3 filename
            safe_fund_id = session_data['safe_fund_id']
            safe_fund_name = session_data['safe_fund_name']
            filename = f"{safe_fund_id}_{safe_fund_name}_slide{slide_id}.png"
            s3_key = f"presentations/{session_id}/{filename}"
            
            try:
                app.logger.info(f"Uploading slide {slide_id} to S3: {s3_key}")
                
                # Upload to S3
                s3_client.put_object(
                    Bucket=S3_BUCKET_NAME,
                    Key=s3_key,
                    Body=image_data,
                    ContentType='image/png',
                    ACL='public-read'
                )
                
                # Generate public URL
                s3_url = f"https://{S3_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
                
                # Group by category
                if category not in uploaded_slides:
                    uploaded_slides[category] = []
                
                uploaded_slides[category].append({
                    'id': slide_id,
                    'filename': filename,
                    's3_url': s3_url,
                    's3_key': s3_key
                })
                
                app.logger.info(f"Successfully uploaded slide {slide_id} to S3: {s3_url}")
                
            except ClientError as e:
                app.logger.error(f"S3 upload error for slide {slide_id}: {str(e)}")
                return jsonify({'error': f'Failed to upload slide {slide_id} to S3: {str(e)}'}), 500
        
        # Generate HTML with real S3 URLs
        html_sections = {}
        
        for category, slides in uploaded_slides.items():
            # Sanitize category name for CSS class to prevent HTML generation errors
            safe_category = sanitize_filename(category.lower()) if category else 'uncategorized'
            
            html_parts = []
            html_parts.append(f'<!-- {category} Section -->')
            html_parts.append(f'<div class="{safe_category}-section">')
            html_parts.append(f'  <h2>{category}</h2>')
            html_parts.append('  <div class="slides-container">')
            
            for slide in slides:
                html_parts.append('    <div class="slide-item">')
                html_parts.append(f'      <img src="{slide["s3_url"]}" alt="{category}_slide{slide["id"]}" />')
                html_parts.append(f'      <p>Slide {slide["id"]}</p>')
                html_parts.append('    </div>')
            
            html_parts.append('  </div>')
            html_parts.append('</div>')
            
            html_sections[category] = '\n'.join(html_parts)
        
        app.logger.info(f"Generated HTML for {len(uploaded_slides)} categories")
        
        # Clean up session data
        del session_storage[session_id]
        
        return jsonify({
            'success': True,
            'uploaded_slides': uploaded_slides,
            'html_sections': html_sections,
            's3_bucket': S3_BUCKET_NAME,
            'total_uploaded': sum(len(slides) for slides in uploaded_slides.values())
        })
        
    except Exception as e:
        app.logger.error(f"Error processing slides: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'File too large. Maximum size is 50MB.'}), 413

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

