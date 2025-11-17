#!/usr/bin/env python3
"""
Wiki.js to AnythingLLM Ingestor

This script imports all Wiki.js content to AnythingLLM,
converting wiki pages to markdown format and uploading them.
"""
import os, sys, re, json, pathlib, shutil, urllib3
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import requests

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# =========================
# ENV Configuration
# =========================
WIKIJS_URL = os.getenv("WIKIJS_URL", "").rstrip("/")
WIKIJS_TOKEN = os.getenv("WIKIJS_TOKEN", "")

# AnythingLLM Configuration
ANYL_BASE_URL = os.getenv("ANYL_BASE_URL", "").rstrip("/")
ANYL_API_KEY = os.getenv("ANYL_API_KEY", "")
ANYL_WORKSPACE = os.getenv("ANYL_WORKSPACE", "default")

# Output directory for markdown files
OUTDIR = pathlib.Path("/app/out_md")
OUT_WIKI_DIR = OUTDIR / "wikijs"
OUTDIR.mkdir(exist_ok=True, parents=True)
OUT_WIKI_DIR.mkdir(exist_ok=True, parents=True)

# TLS Configuration
INSECURE_SSL = os.getenv("INSECURE_SSL", "false").lower() in ("1", "true", "yes")
CA_BUNDLE = os.getenv("CA_BUNDLE", "").strip()

if INSECURE_SSL and not CA_BUNDLE:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    REQUESTS_VERIFY = False
elif CA_BUNDLE:
    REQUESTS_VERIFY = CA_BUNDLE
else:
    REQUESTS_VERIFY = True

# Validation
if not WIKIJS_URL:
    raise RuntimeError("WIKIJS_URL is empty")
if not WIKIJS_TOKEN:
    raise RuntimeError("WIKIJS_TOKEN is empty")
if not ANYL_BASE_URL or not ANYL_API_KEY:
    raise RuntimeError("ANYL_BASE_URL and ANYL_API_KEY are required")

# =========================
# Utility Functions
# =========================
def log(msg: str):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)

def slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.U).strip().lower()
    return re.sub(r"[-\s]+", "-", text)

def ensure_ok(r: requests.Response):
    try:
        r.raise_for_status()
    except Exception:
        log(f"HTTP {r.status_code}: {r.text[:200]}")
        raise

# =========================
# Wiki.js Client
# =========================
class WikiJS:
    def __init__(self, base_url: str, token: str):
        self.base = base_url
        self.s = requests.Session()
        self.s.verify = REQUESTS_VERIFY
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        })

    def post_graphql(self, query: str, variables: dict = None) -> requests.Response:
        return self.s.post(
            f"{self.base}/graphql",
            json={"query": query, "variables": variables or {}},
            timeout=40,
        )

    def try_endpoints(self, query: str, variables: dict):
        """Try /graphql then /api/graphql, return first (data, endpoint) that works."""
        for endpoint_suffix in ("/graphql", "/api/graphql"):
            url = f"{self.base}{endpoint_suffix}"
            r = self.s.post(url, json={"query": query, "variables": variables or {}}, timeout=40)
            if r.ok:
                data = r.json().get("data")
                if data is not None:
                    return data, url
            last_r = r
        last_r.raise_for_status()
        return None, None

# =========================
# AnythingLLM Client
# =========================
class AnythingLLM:
    def __init__(self, base_url: str, api_key: str, workspace: str):
        if not base_url or not api_key:
            raise SystemExit("ANYL_BASE_URL/ANYL_API_KEY missing")
        self.base = base_url
        self.ws = workspace
        self.s = requests.Session()
        self.s.verify = REQUESTS_VERIFY
        self.s.headers.update({
            "Authorization": f"Bearer {api_key}",
            "X-AnythingLLM-Access-Token": api_key,
            "User-Agent": "rag-ingestor/1.3"
        })
        self.paths = self._discover_paths()

    def _fetch_openapi(self) -> Optional[dict]:
        for path in ("/api/docs-json", "/api/openapi.json", "/api/docs.json"):
            try:
                r = self.s.get(self.base + path, timeout=5)
                if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
                    return r.json()
            except Exception:
                pass
        return None

    def _discover_paths(self) -> Dict[str, str]:
        """Discover API endpoints for workspace-scoped and global operations."""
        # Use the correct endpoint from GitHub issue #2947
        paths = {
            "upload_ws": "/api/v1/document/upload",  # Correct endpoint (singular)
            "upload_user": "/api/v1/document/upload",
            "attach": "/api/v1/workspaces/{workspace}/documents/attach",
            "embed": "/api/v1/workspaces/{workspace}/documents/embed", 
            "list_ws_docs": "/api/v1/workspaces/{workspace}/documents"
        }
        return paths

    def _fmt(self, path: str) -> str:
        return self.base + path.replace("{workspace}", self.ws).replace(":workspace", self.ws)

    @staticmethod
    def _extract_ids(data: Any) -> List[str]:
        if data is None:
            return []
        if isinstance(data, dict):
            for key in ("ids", "document_ids", "documents_ids"):
                if key in data and isinstance(data[key], list):
                    return [str(x.get("id", x)) for x in data[key]]
            for key in ("id", "document_id", "documentId"):
                if key in data:
                    return [str(data[key])]
            if "documents" in data and isinstance(data["documents"], list):
                out = []
                for d in data["documents"]:
                    if isinstance(d, dict) and "id" in d:
                        out.append(str(d["id"]))
                return out
        return []

    def _try_upload(self, url: str, file_path: pathlib.Path) -> List[str]:
        """Try upload with different multipart keys and robust error handling."""
        # Try different multipart keys (some builds want "files" or "documents")
        keys = ("file", "files", "document", "documents")
        for key in keys:
            try:
                files = {key: (file_path.name, open(file_path, "rb"), "text/markdown")}
                r = self.s.post(url, files=files, timeout=180)
            except Exception as e:
                log(f"[upload] {file_path.name} EXC su {url}: {e}")
                continue
            # Accept only 2xx
            if r.status_code // 100 != 2:
                log(f"[upload] {file_path.name} -> {url} HTTP {r.status_code} body={r.text[:200]}")
                continue
            # Parse ids
            try:
                data = r.json() if r.headers.get("Content-Type","").startswith("application/json") else None
            except Exception:
                data = None
            ids = self._extract_ids(data)
            if ids:
                return ids
            # If 2xx but empty, debug log
            log(f"[upload] {file_path.name} -> {url} 2xx ma senza id. Body={r.text[:200]}")
        return []

    def upload_to_workspace_or_user(self, file_path: pathlib.Path) -> List[str]:
        """Try workspace upload first, then user library. Return list of IDs."""
        # 1) workspace-scoped
        ids = self._try_upload(self._fmt(self.paths["upload_ws"]), file_path)
        if ids:
            return ids
        # 2) fallback: user library
        ids = self._try_upload(self.base + self.paths["upload_user"], file_path)
        if ids:
            return ids
        # 3) legacy fallback extra
        for legacy in (
            f"/api/workspaces/{self.ws}/documents/upload",
            f"/api/workspace/{self.ws}/upload",
        ):
            ids = self._try_upload(self.base + legacy, file_path)
            if ids:
                return ids
        return []

    def attach_documents_to_workspace(self, doc_ids: List[str]) -> bool:
        """Attach documents to workspace."""
        if not doc_ids:
            return True
        
        url = self._fmt(self.paths["attach"])
        payload = {"document_ids": doc_ids}
        r = self.s.post(url, json=payload, timeout=60)
        ok = r.status_code // 100 == 2
        if not ok:
            log(f"[attach] HTTP {r.status_code}: {r.text[:300]}")
        return ok

    def embed_documents(self, doc_ids: List[str]) -> bool:
        """Trigger embedding for documents."""
        if not doc_ids:
            return True
        
        url = self._fmt(self.paths["embed"])
        r = self.s.post(url, timeout=60)
        return r.status_code // 100 == 2

# =========================
# Wiki.js GraphQL Queries
# =========================
# Two common schema variants in Wiki.js:
# Variant A (nested under `pages`):
PAGES_A = """
query Pages($limit:Int!){
  pages { list(limit:$limit){ id title path updatedAt } }
}
"""
PAGE_A = """
query Page($id:Int!){
  pages { single(id:$id){ id title path content updatedAt } }
}
"""

# Variant B (root-level fields):
PAGES_B = """
query Pages($limit:Int!){
  pages(limit:$limit){ id title path updatedAt }
}
"""
PAGE_B = """
query Page($id:Int!){
  page(id:$id){ id title path content updatedAt }
}
"""


def md_wiki_page(page: Dict[str, Any]) -> str:
    """Convert Wiki.js page to markdown format."""
    title = page.get("title", "Untitled")
    content = page.get("content", "")
    path = page.get("path", "")
    updated_at = page.get("updatedAt", "")
    page_id = page.get("id", "")
    
    # Create markdown content
    md_content = f"""# {title}

**Path:** `{path}`  
**Page ID:** {page_id}  
**Last Updated:** {updated_at}  
**Source:** Wiki.js  
**URL:** {WIKIJS_URL}/{path}

---

{content}
"""
    return md_content

def export_wikijs() -> int:
    """Export all Wiki.js pages to markdown files."""
    if not WIKIJS_URL or not WIKIJS_TOKEN:
        log("Skipping Wiki.js export (missing credentials)")
        return 0
        
    wikijs = WikiJS(WIKIJS_URL, WIKIJS_TOKEN)
    total_files = 0
    
    log("Fetching Wiki.js pages...")
    
    # Detect variant and fetch pages
    limit = 100
    detected = None
    endpoint = None
    
    # Detect variant with first page
    try:
        # Try Variant A
        r = wikijs.post_graphql(PAGES_A, {"limit": limit})
        if r.ok:
            j = r.json()
            if j.get("data") and j["data"].get("pages") and isinstance(j["data"]["pages"].get("list"), list):
                detected = "A"
                endpoint = f"{WIKIJS_URL}/graphql"
                pages = j["data"]["pages"]["list"]
            else:
                # Try Variant B
                r = wikijs.post_graphql(PAGES_B, {"limit": limit})
                if r.ok:
                    j = r.json()
                    if j.get("data") and isinstance(j["data"].get("pages"), list):
                        detected = "B"
                        endpoint = f"{WIKIJS_URL}/graphql"
                        pages = j["data"]["pages"]
                    else:
                        raise RuntimeError("Could not detect Wiki.js schema variant")
                else:
                    raise RuntimeError("Wiki.js GraphQL failed")
        else:
            raise RuntimeError("Wiki.js GraphQL failed")
    except Exception as e:
        log(f"Error detecting Wiki.js variant: {e}")
        return 0
    
    if not pages:
        log("No pages found")
        return 0
    
    # Process pages
    for page in pages:
        try:
            # Fetch full page content
            page_id = page["id"]
            query = PAGE_A if detected == "A" else PAGE_B
            r = wikijs.post_graphql(query, {"id": page_id})
            if not r.ok:
                log(f"Failed to fetch page {page_id}: {r.status_code}")
                continue
            
            j = r.json()["data"]
            if detected == "A":
                full_page = j["pages"]["single"]
            else:
                full_page = j["page"]
            
            # Convert to markdown and save
            title = full_page.get("title", "Untitled")
            fname = OUT_WIKI_DIR / f"{page_id}-{slug(title)[:80]}.md"
            fname.write_text(md_wiki_page(full_page), encoding="utf-8")
            total_files += 1
            
        except Exception as e:
            log(f"Error processing page {page.get('id', 'unknown')}: {e}")
            continue
    
    log(f"Exported {total_files} Wiki.js pages to {OUTDIR}")
    return total_files

def push_to_anythingllm() -> int:
    """Upload markdown files to AnythingLLM."""
    client = AnythingLLM(ANYL_BASE_URL, ANYL_API_KEY, ANYL_WORKSPACE)
    
    # Upload files
    uploaded_ids: List[str] = []
    for path in sorted(OUT_WIKI_DIR.glob("*.md")):
        ids = client.upload_to_workspace_or_user(path)
        if ids:
            uploaded_ids.extend(ids)
        else:
            log(f"Failed to upload {path.name}")
    
    if not uploaded_ids:
        log("No files uploaded successfully")
        return 0
    
    # Attach to workspace
    if client.attach_documents_to_workspace(uploaded_ids):
        log(f"Attached {len(uploaded_ids)} documents to workspace")
    else:
        log("Failed to attach documents to workspace")
    
    # Trigger embedding
    if client.embed_documents(uploaded_ids):
        log(f"Triggered embedding for {len(uploaded_ids)} documents")
    else:
        log("Failed to trigger embedding")
    
    return len(uploaded_ids)

def main():
    """Main function to export Wiki.js content to AnythingLLM."""
    if not WIKIJS_URL or not WIKIJS_TOKEN:
        log("Skipping Wiki.js ingestion (missing credentials: WIKIJS_URL/WIKIJS_TOKEN)")
        return
    if not ANYL_BASE_URL or not ANYL_API_KEY:
        log("Skipping Wiki.js ingestion (missing credentials: ANYL_BASE_URL/ANYL_API_KEY)")
        return

    # Clean output directory
    if OUT_WIKI_DIR.exists():
        shutil.rmtree(OUT_WIKI_DIR)
    OUT_WIKI_DIR.mkdir(parents=True, exist_ok=True)

    # Export Wiki.js content
    total = export_wikijs()
    if total == 0:
        log("No Wiki.js pages found")
        return

    # Upload to AnythingLLM using correct API endpoint
    log("Wiki.js export completed. Starting upload to AnythingLLM...")
    try:
        attached = push_to_anythingllm()
        if attached > 0:
            log(f"Successfully uploaded {attached} Wiki.js files to AnythingLLM")
        else:
            log("No files were uploaded to AnythingLLM")
    except Exception as e:
        log(f"Upload error: {e}")
        log(f"Files saved to: {OUTDIR}")
    
    log(f"Completed: exported {total} Wiki.js pages to markdown files")

if __name__ == "__main__":
    main()
