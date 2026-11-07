# Runtime secrets

Docker Compose expects the following files in this directory:

- `mongo_root_password`
- `mongo_app_password`
- `mongo_uri`
- `bot_token`
- `api_hash`
- `session_encryption_key`

Each file contains only the secret value, with no variable name or quotes.

Generate the Fernet session key with:

`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
