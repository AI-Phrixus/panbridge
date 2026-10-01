"""Loopback-only UI acceptance fixture. Fictional data; no account/cloud access."""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
env = Environment(loader=FileSystemLoader(ROOT / "web/templates"), autoescape=True)
jobs = {
    9001: dict(id=9001, title="介面驗收用・935 文件（模擬資料）", status="awaiting_selection", progress=0),
    9002: dict(id=9002, title="介面驗收用・可暫停任務（模擬資料）", status="downloading", progress=30),
}
for job in jobs.values():
    job.update(source_type="quark", destination="google", share_url="https://example.invalid/s/demo",
               pcloud_path="/PanBridge/模擬資料", speed_bps=1048576, status_detail="模擬資料；不會連接網盤或下載", error_message="")
files = [dict(id=i+1, relative_path=f"模擬資料/第{i//100+1}組/影片 {i+1:04}.mp4", remote_name=f"影片{i+1}.mp4",
              size=21*1024*1024, status="queued", downloaded_bytes=0, uploaded_bytes=0,
              error_message="", pcloud_path="") for i in range(935)]
mutations = []

class Preview(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def respond(self, body, content_type="application/json", status=200):
        if content_type == "application/json": body=json.dumps(body,ensure_ascii=False).encode()
        elif isinstance(body,str): body=body.encode()
        self.send_response(status);self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(body)));self.send_header("Cache-Control","no-store")
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=="/": return self.respond(env.get_template("index.html").render(),"text/html; charset=utf-8")
        if path in ("/tasks/9001","/tasks/9002"):
            return self.respond(env.get_template("task.html").render(job_id=int(path.rsplit('/',1)[1])),"text/html; charset=utf-8")
        if path in ("/static/ui.js","/static/style.css"):
            return self.respond((ROOT/"web"/path.lstrip('/')).read_bytes(),"text/javascript" if path.endswith('.js') else "text/css")
        if path=="/api/tasks":return self.respond({"jobs":list(jobs.values())})
        if path=="/api/tasks/system/status":return self.respond(dict(version="0.5.4 DEMO",disk_free_gb=140,max_concurrent_jobs=2,providers={"google":True,"quark":True}))
        if path=="/test-observations":return self.respond({"mutations":mutations,"jobs":list(jobs.values())})
        match=re.fullmatch(r"/api/tasks/(900[12])",path)
        if match:
            job=jobs.get(int(match[1]))
            return self.respond({"job":job,"files":files if job['id']==9001 else []}) if job else self.respond({"detail":"not found"},status=404)
        return self.respond({"detail":"fixture route not found"},status=404)
    def do_POST(self):
        path=urlsplit(self.path).path;body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
        match=re.fullmatch(r"/api/tasks/(900[12])/(pause|resume|cancel|selection)",path)
        if not match:return self.respond({"detail":"fixture mutation not allowed"},status=400)
        jid=int(match[1]);action=match[2];mutations.append({"job":jid,"action":action,"file_ids":body.get('file_ids',[])})
        jobs[jid]['status']={'pause':'paused','resume':'downloading','cancel':'cancelled','selection':'downloading'}[action]
        if action=='selection':
            selected=set(body.get('file_ids',[]))
            for file in files:file['status']='queued' if file['id'] in selected else 'skipped'
        self.respond({"ok":True})

if __name__=="__main__":
    print("Fictional UI fixture on http://127.0.0.1:18785 (no cloud access)",flush=True)
    ThreadingHTTPServer(("127.0.0.1",18785),Preview).serve_forever()
