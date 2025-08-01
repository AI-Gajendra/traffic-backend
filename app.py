#simple flask server
from flask import Flask, request, jsonify

def create_app():
    app = Flask(__name__)

    users = []

    @app.route('/')
    def index():
        return jsonify({"message": "Hello, World!"})

    @app.route('/signup', methods=['POST'])
    def signup():
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        if not username or not password:
            return jsonify({"error": "Username and password required"}), 400
        # Simple check for existing user
        if any(u['username'] == username for u in users):
            return jsonify({"error": "User already exists"}), 409
        users.append({'username': username, 'password': password})
        return jsonify({"message": "User signed up successfully"}), 201

    @app.route('/login', methods=['POST'])
    def login():
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        if not username or not password:
            return jsonify({"error": "Username and password required"}), 400
        user = next((u for u in users if u['username'] == username and u['password'] == password), None)
        if not user:
            return jsonify({"error": "Invalid username or password"}), 401
        return jsonify({"message": "Login successful"}), 200

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000)