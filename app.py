import os
from flask import Flask, request, render_template, jsonify
from PIL import Image
import time
import logging
import numpy as np
import umap
import plotly.express as px
# --- Configuration ---
UPLOAD_FOLDER = 'static/uploads/'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
IMAGE_DATA_DIR = 'static/images/'
ELASTIC_INDEX = 'image_search_index'

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure the upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Global variables for lazy loading
model = None
es = None

# --- Lazy Loading Functions ---
def get_model():
    """Loads the SentenceTransformer model only when needed."""
    global model
    if model is None:
        logging.info("Loading CLIP model... (This may take a few minutes on first run)")
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("sentence-transformers/clip-ViT-B-32")
        logging.info("Model loaded successfully.")
    return model

def get_es_client():
    """Initializes and returns an Elasticsearch client, waiting if necessary."""
    global es
    if es is None:
        from elasticsearch import Elasticsearch, ConnectionError
        
        host = os.environ.get("ELASTICSEARCH_HOST", "http://localhost:9200")
        logging.info(f"Attempting to connect to Elasticsearch at {host}...")
        
        es_client = Elasticsearch(hosts=[host], request_timeout=30)

        for i in range(30):
            try:
                if es_client.ping():
                    logging.info("Successfully connected to Elasticsearch.")
                    es = es_client
                    return es
            except ConnectionError:
                logging.info(f"Waiting for Elasticsearch... attempt {i+1}/30")
                time.sleep(2)
        
        raise ConnectionError("Could not connect to Elasticsearch after multiple attempts.")
    return es

# --- Helper Functions ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def create_index_if_not_exists():
    es_client = get_es_client()
    if not es_client.indices.exists(index=ELASTIC_INDEX):
        mapping = {
            "properties": {
                "image_vector": {"type": "dense_vector", "dims": 512, "index": True, "similarity": "cosine"},
                "image_path": {"type": "keyword"}
            }
        }
        logging.info(f"Creating index '{ELASTIC_INDEX}'.")
        es_client.indices.create(index=ELASTIC_INDEX, mappings=mapping)
    else:
        logging.info(f"Index '{ELASTIC_INDEX}' already exists.")

# --- Routes ---
@app.route('/index_images', methods=['POST'])
def index_images():
    try:
        create_index_if_not_exists()
        current_model = get_model()
        es_client = get_es_client()
        
        if not os.path.exists(IMAGE_DATA_DIR):
            return jsonify({"error": f"Directory {IMAGE_DATA_DIR} does not exist"}), 400
        
        indexed_count = 0
        for filename in os.listdir(IMAGE_DATA_DIR):
            if allowed_file(filename):
                img_path = os.path.join(IMAGE_DATA_DIR, filename)
                search_body = {"query": {"term": {"image_path": img_path}}}
                if es_client.count(index=ELASTIC_INDEX, body=search_body)['count'] > 0:
                    logging.info(f"Image {filename} already indexed. Skipping.")
                    continue
                
                img = Image.open(img_path)
                embedding = current_model.encode(img).tolist()
                doc = {'image_path': img_path, 'image_vector': embedding}
                es_client.index(index=ELASTIC_INDEX, document=doc, refresh=True)
                indexed_count += 1
                logging.info(f"Indexed {filename}")
        
        return jsonify({"message": f"Successfully indexed {indexed_count} new images."})
    
    except Exception as e:
        logging.error(f"Error in index_images: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500



@app.route('/', methods=['GET', 'POST'])
def search_page():
    # Initialize all possible variables that can be sent to the template
    results = []
    query_image_path = None
    text_query = ""
    error_message = None

    # --- Only process form data if the request is a POST ---
    if request.method == 'POST':
        try:
            current_model = get_model()
            es_client = get_es_client()
            query_vector = None
            
            # --- SCENARIO 1: The user submitted the text search form ---
            if 'text_query' in request.form and request.form['text_query']:
                text_query = request.form['text_query'].strip()
                logging.info(f"Performing text search for: '{text_query}'")
                
                # Use the same CLIP model to encode the TEXT description
                query_vector = current_model.encode(text_query).tolist()

            # --- SCENARIO 2: The user uploaded an image file ---
            elif 'file' in request.files and request.files['file'].filename != '':
                file = request.files['file']
                if allowed_file(file.filename):
                    filename = f"{time.time()}_{file.filename}"
                    query_image_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(query_image_path)
                    
                    logging.info(f"Performing image search for: '{query_image_path}'")
                    img = Image.open(query_image_path)
                    
                    # Use the same CLIP model to encode the IMAGE
                    query_vector = current_model.encode(img).tolist()
                else:
                    # Handle cases where the file is not an allowed type
                    error_message = "Invalid file type. Please upload a PNG, JPG, or JPEG."
                    return render_template('index.html', error=error_message)
            
            # --- SCENARIO 3: The POST request was empty ---
            else:
                error_message = "Please either upload an image or enter a text description to search."
                return render_template('index.html', error=error_message)

            # --- Perform the KNN search (this part runs for both text and image) ---
            if query_vector:
                knn_query = {"field": "image_vector", "query_vector": query_vector, "k": 5, "num_candidates": 100}
                response = es_client.search(index=ELASTIC_INDEX, knn=knn_query, source=["image_path"])

                results = [{
                    "path": hit['_source']['image_path'],
                    "score": hit['_score']
                } for hit in response['hits']['hits']]

        # --- General Error Handling ---
        except Exception as e:
            logging.error(f"An error occurred during search: {e}", exc_info=True)
            error_message = f"An error occurred: {e}"
            # Still render the main page, but show the error and the user's original query
            return render_template('index.html', error=error_message, query_image=query_image_path, text_query=text_query)

    # --- This runs for the initial GET request and after a successful POST search ---
    return render_template('index.html', results=results, query_image=query_image_path, text_query=text_query, error=error_message)





@app.route('/visualize')
def visualize_vectors():
    try:
        es_client = get_es_client()
        
        if not es_client.indices.exists(index=ELASTIC_INDEX) or es_client.count(index=ELASTIC_INDEX)['count'] == 0:
            return "No images have been indexed yet. Please go back and index your images first.", 404

        response = es_client.search(
            index=ELASTIC_INDEX,
            query={"match_all": {}},
            size=10000 
        )
        
        hits = response['hits']['hits']
        if not hits:
            return "No vectors found in the index.", 404

        vectors = [hit['_source']['image_vector'] for hit in hits]
        image_paths = [hit['_source']['image_path'] for hit in hits]
        
        vectors_np = np.array(vectors)

        logging.info(f"Running UMAP on {len(vectors_np)} vectors...")
        reducer = umap.UMAP(n_components=2, random_state=42)
        embedding_2d = reducer.fit_transform(vectors_np)
        logging.info("UMAP processing complete.")

        fig = px.scatter(
            x=embedding_2d[:, 0],
            y=embedding_2d[:, 1],
            hover_name=image_paths,
            title="Image Vector Clusters (UMAP Projection)",
            labels={'x': 'UMAP Dimension 1', 'y': 'UMAP Dimension 2'}
        )
        fig.update_traces(marker=dict(size=8, opacity=0.8))
        
        plot_div = fig.to_html(full_html=False)

        return render_template('visualize.html', plot_div=plot_div)

    except Exception as e:
        logging.error(f"Error creating visualization: {e}", exc_info=True)
        return f"An error occurred while creating the visualization: {e}", 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port, use_reloader=False)