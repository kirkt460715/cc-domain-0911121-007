import argparse,json,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        routes=json.loads((ROOT/'fixtures/routes.json').read_text())
        route=routes.get(self.path) or routes.get(urlsplit(self.path).path)
        if route is None:
            self.send_error(404);return
        data=(ROOT/'fixtures'/route['file']).read_bytes() if 'file' in route else json.dumps(route.get('body',{}),ensure_ascii=False).encode()
        if route.get('truncate'):data=data[:-route['truncate']]
        self.send_response(route.get('status',200))
        self.send_header('Content-Type',route.get('type','application/json'))
        for k,v in route.get('headers',{}).items():self.send_header(k,v)
        self.send_header('Content-Length',str(len(data)));self.end_headers()
        size=route.get('chunk',len(data) or 1)
        try:
            for start in range(0,len(data),size):self.wfile.write(data[start:start+size]);self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError):pass
def start():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    return server,'http://127.0.0.1:'+str(server.server_port)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8787);a=p.parse_args()
    with ThreadingHTTPServer(('127.0.0.1',a.port),Handler) as s:
        print('本地来源已启动，端口',s.server_port,flush=True);s.serve_forever()
