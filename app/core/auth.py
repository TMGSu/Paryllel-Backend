from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt import PyJWKClient
import jwt
import os
from dotenv import load_dotenv

load_dotenv()

security = HTTPBearer()

CLERK_JWKS_URL = f"https://{os.getenv('CLERK_FRONTEND_API')}/.well-known/jwks.json"

jwk_client = PyJWKClient(CLERK_JWKS_URL)


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials

    try:
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_exp": True}
        )
        return payload  # ✅ full payload, not just sub

    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")