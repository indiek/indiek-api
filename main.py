import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
import networkx as nx

# If you need on-the-fly TIFF conversion for the browser, Pillow is highly effective
try:
    from PIL import Image
    import io
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

app = FastAPI(title="Graph Data API")

# Configure CORS to allow Next.js (typically running on localhost:3000) to fetch data
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------------------
# 1. Global State & Data Loading
# -------------------------------------------------------------------

# You can replace this initialization with your existing Python logic.
# The assumption is G contains the base nodes and edges, and node data includes attached tags.
G = nx.DiGraph()
tags_db = {} 

def load_graph_data():
    """
    Placeholder for your existing parsing logic. 
    You will populate `G` and `tags_db` here.
    """
    global G, tags_db
    
    # Example Tag Dictionary format expected by the frontend
    tags_db = {
        "019f45e2-afb0-7521-bd6a-9399af9329a6": {
            "name": "reaction",
            "description": "reaction to others' words"
        },
        "019f45e2-b14c-77b2-a995-369947400acc": {
            "name": "discussion",
            "description": "discussion about God"
        }
    }
    
    # Example NetworkX population
    # Important: attach the list of tag UUIDs to the node attributes
    G.add_node("019f45e2-ac75-7522-8cf9-8c9faa4f851e", 
               blob_hash="9aa9c8a7366899a159fe7cc523037b24f96def9f60a45f367d91a63ed694bf3a",
               tags=["019f45e2-b14c-77b2-a995-369947400acc"])
    
    G.add_node("019f45e2-ae04-72c9-8ac0-3eeda726daa9", 
               blob_hash="996d0722facdeae9c1db6b25ab581046328a9b2244be1cbf7f64ddcfc82ed907",
               tags=[])
    
    G.add_edge("019f45e2-ae04-72c9-8ac0-3eeda726daa9", 
               "019f45e2-ac75-7522-8cf9-8c9faa4f851e", 
               id="019f45e2-aeda-7253-92e4-d650a05e480b",
               tags=["019f45e2-afb0-7521-bd6a-9399af9329a6"])

# Load data into RAM on server startup
@app.on_event("startup")
def startup_event():
    load_graph_data()

# -------------------------------------------------------------------
# 2. API Endpoints
# -------------------------------------------------------------------

@app.get("/api/graph")
def get_graph():
    """
    Translates the NetworkX graph into the strict JSON schema 
    required by `react-force-graph`: { "nodes": [...], "links": [...] }
    """
    nodes = []
    for node_id, data in G.nodes(data=True):
        nodes.append({
            "id": node_id,
            "blob_hash": data.get("blob_hash"),
            "tags": data.get("tags", [])
        })
        
    links = []
    for source, target, data in G.edges(data=True):
        links.append({
            "id": data.get("id"),
            "source": source,
            "target": target,
            "tags": data.get("tags", [])
        })
        
    return {"nodes": nodes, "links": links}

@app.get("/api/tags")
def get_tags():
    """
    Returns the dictionary of tags. 
    The Next.js front-end will use this to populate its search-first dashboard 
    and resolve UUIDs to human-readable names.
    """
    return tags_db

@app.get("/api/blob/{hash_str}")
def get_blob(hash_str: str):
    """
    Resolves the sha256 hash to the physical file in the nested archive directory.
    If the file is a TIFF image, it optionally converts it to WebP for the browser.
    """
    # Build path: archive/blobs/sha256/xx/yy/hash
    dir1 = hash_str[0:2]
    dir2 = hash_str[2:4]
    
    # Use your project's absolute or relative root path
    base_dir = Path("archive/blobs/sha256")
    file_path = base_dir / dir1 / dir2 / hash_str
    
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Blob not found")

    # Simple heuristic to handle TIFFs if Pillow is installed
    if HAS_PILLOW:
        try:
            with Image.open(file_path) as img:
                if img.format in ['TIFF', 'TIF']:
                    # Convert to a browser-friendly format in memory
                    img_byte_arr = io.BytesIO()
                    img.save(img_byte_arr, format='WEBP')
                    img_byte_arr.seek(0)
                    return Response(content=img_byte_arr.getvalue(), media_type="image/webp")
        except Exception:
            # If Pillow fails to parse it as an image, fall back to serving it as plain text/binary
            pass

    # For text blobs or if no conversion is needed
    return FileResponse(file_path)

if __name__ == "__main__":
    import uvicorn
    # Run the server on port 8000
    uvicorn.run(app, host="0.0.0.0", port=8000)
