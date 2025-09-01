import os
import tempfile
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.utils import secure_filename
import PyPDF2
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

# AWS S3 Configuration (will be set via environment variables)
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
S3_BUCKET_NAME = os.getenv('S3_BUCKET_NAME')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def convert_pdf_to_images(pdf_path):
    """Convert PDF to images and return as base64 encoded thumbnails"""
    try:
        # Convert PDF pages to images
        images = convert_from_path(pdf_path, dpi=150, fmt='PNG')
        
        slides = []
        for i, image in enumerate(images):
            # Create thumbnail for preview
            thumbnail = image.copy()
            thumbnail.thumbnail((300, 200), Image.Resampling.LANCZOS)
            
            # Convert thumbnail to base64
            buffer = BytesIO()
            thumbnail.save(buffer, format='PNG')
            thumbnail_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            slides.append({
                'id': i + 1,
                'thumbnail': f'data:image/png;base64,{thumbnail_b64}',
                'title': f'Slide {i + 1}',
                'selected': False,
                'category': None
            })
        
        return slides
    except Exception as e:
        print(f"Error converting PDF: {str(e)}")
        return []

def upload_to_s3(file_data, filename):
    """Upload file to S3 and return public URL"""
    if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET_NAME]):
        return None
    
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION
        )
        
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=filename,
            Body=file_data,
            ContentType='image/png',
            ACL='public-read'
        )
        
        return f'https://{S3_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com/{filename}'
    except ClientError as e:
        print(f"Error uploading to S3: {str(e)}")
        return None

@app.route('/')
def home():
    """Root endpoint - shows API is running"""
    return jsonify({
        "message": "Slide Parser API is running!",
        "status": "healthy",
        "platform": "heroku",
        "version": "1.0.0",
        "endpoints": [
            "/health - Health check",
            "/api/test - API status", 
            "/api/upload - Upload PDF files",
            "/api/generate-html - Generate HTML from slides"
        ]
    })

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "platform": "heroku"})

@app.route('/api/upload', methods=['POST'])
def upload_pdf():
    """Upload and process PDF file"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        fund_id = request.form.get('fund_id', '')
        fund_name = request.form.get('fund_name', '')
        
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'error': 'Invalid file type. Only PDF files are allowed.'}), 400
        
        # Create temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as temp_file:
            file.save(temp_file.name)
            
            # Process PDF
            slides = convert_pdf_to_images(temp_file.name)
            
            # Clean up temp file
            os.unlink(temp_file.name)
        
        if not slides:
            return jsonify({'error': 'Failed to process PDF'}), 500
        
        # Generate session ID
        session_id = str(uuid.uuid4())
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'slides': slides,
            'fund_id': fund_id,
            'fund_name': fund_name,
            'total_slides': len(slides)
        })
    
    except Exception as e:
        print(f"Error processing upload: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/generate-html', methods=['POST'])
def generate_html():
    """Generate HTML code from selected slides"""
    try:
        data = request.get_json()
        selected_slides = data.get('slides', [])
        fund_id = data.get('fund_id', '')
        fund_name = data.get('fund_name', '')
        categories = data.get('categories', {})
        
        if not selected_slides:
            return jsonify({'error': 'No slides selected'}), 400
        
        # Group slides by category
        categorized_slides = {}
        for slide in selected_slides:
            category = slide.get('category', 'uncategorized')
            if category not in categorized_slides:
                categorized_slides[category] = []
            categorized_slides[category].append(slide)
        
        # Generate HTML for each category
        html_sections = {}
        
        for category, slides in categorized_slides.items():
            category_name = categories.get(category, category.title())
            
            # Basic HTML template
            basic_html = f"""
<!-- {category_name} Section ({len(slides)} slides) -->
<div class="slide-section" data-category="{category}">
    <h2>{category_name}</h2>
    <div class="slides-container">
"""
            
            for slide in slides:
                slide_filename = f"{fund_id}_{fund_name.replace(' ', '_')}_{category}_{slide['id']}.png"
                basic_html += f"""
        <div class="slide-item">
            <img src="https://your-s3-bucket.s3.amazonaws.com/{slide_filename}" 
                 alt="{category_name} Slide {slide['id']}" 
                 class="slide-image">
            <p class="slide-caption">Slide {slide['id']}</p>
        </div>
"""
            
            basic_html += """
    </div>
</div>

<style>
.slide-section {
    margin: 20px 0;
    padding: 20px;
    border: 1px solid #ddd;
    border-radius: 8px;
}

.slides-container {
    display: flex;
    flex-wrap: wrap;
    gap: 15px;
}

.slide-item {
    flex: 0 0 300px;
    text-align: center;
}

.slide-image {
    width: 100%;
    height: auto;
    border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
}

.slide-caption {
    margin-top: 8px;
    font-size: 14px;
    color: #666;
}
</style>
"""
            
            html_sections[category] = {
                'basic': basic_html,
                'modal': basic_html,  # Simplified for now
                'grid': basic_html    # Simplified for now
            }
        
        return jsonify({
            'success': True,
            'html_sections': html_sections,
            'categories': categories
        })
    
    except Exception as e:
        print(f"Error generating HTML: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/test')
def test():
    """Test endpoint to verify API is working"""
    return jsonify({
        'message': 'Slide Parser API is working!',
        'platform': 'heroku',
        'environment': os.getenv('FLASK_ENV', 'development'),
        'aws_configured': bool(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and S3_BUCKET_NAME)
    })

@app.errorhandler(413)
def too_large(e):
    return jsonify({'error': 'File too large. Maximum size is 50MB.'}), 413

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500

# Heroku automatically sets PORT environment variable
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

