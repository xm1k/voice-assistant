from http.server import HTTPServer, BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hello from service3!\n")

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print("service3 listening on :8080")
    HTTPServer(("", 8080), Handler).serve_forever()
