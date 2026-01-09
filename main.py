import os
from typing import List

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from tokens import load_tokens, save_tokens

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "0"

app = FastAPI()

# ======================
# Google OAuth config
# ======================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
BASE_URL = os.environ.get("BASE_URL")
REDIRECT_URI = f"{BASE_URL}/auth/google/callback"


def get_oauth_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )


# ======================
# Helpers
# ======================

def get_user_id(payload: dict):
    return payload.get("meta", {}).get("user_id", "default")


def auth_error(id_, user_id):
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {
            "code": 401,
            "message": f"Google Sheets not connected for user '{user_id}'. Visit {BASE_URL}/auth/google?user_id={user_id}",
        },
    }


# ======================
# OAuth routes
# ======================

@app.get("/auth/google")
def google_auth(user_id: str = "default"):
    flow = get_oauth_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=user_id,
    )
    return RedirectResponse(auth_url)


@app.get("/auth/google/callback")
def google_callback(request: Request):
    flow = get_oauth_flow()
    flow.fetch_token(authorization_response=str(request.url))

    user_id = request.query_params.get("state", "default")

    tokens = load_tokens()
    existing_refresh = tokens.get(user_id, {}).get("refresh_token")

    tokens[user_id] = {
        "token": flow.credentials.token,
        "refresh_token": flow.credentials.refresh_token or existing_refresh,
    }
    save_tokens(tokens)

    return {"status": "sheets connected successfully", "user": user_id}


# ======================
# Sheets helpers
# ======================

def get_sheets_service(user_id: str):
    tokens = load_tokens()
    if user_id not in tokens:
        return None

    creds = Credentials(
        token=tokens[user_id]["token"],
        refresh_token=tokens[user_id]["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )

    return build("sheets", "v4", credentials=creds)


def sheets_read_range(user_id: str, spreadsheet_id: str, range_: str):
    service = get_sheets_service(user_id)
    if not service:
        return "AUTH_REQUIRED"

    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_)
        .execute()
    )

    return result.get("values", [])


def sheets_write_range(
    user_id: str,
    spreadsheet_id: str,
    range_: str,
    values: List[List[str]],
):
    service = get_sheets_service(user_id)
    if not service:
        return "AUTH_REQUIRED"

    body = {
        "values": values
    }

    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_,
        valueInputOption="RAW",
        body=body,
    ).execute()

    return "UPDATED"


def sheets_append_row(
    user_id: str,
    spreadsheet_id: str,
    range_: str,
    values: List[str],
):
    service = get_sheets_service(user_id)
    if not service:
        return "AUTH_REQUIRED"

    body = {
        "values": [values]
    }

    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range_,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body=body,
    ).execute()

    return "APPENDED"


# ======================
# MCP endpoint
# ======================

@app.post("/mcp")
async def mcp_handler(request: Request):
    payload = await request.json()
    method = payload.get("method")
    id_ = payload.get("id")
    user_id = get_user_id(payload)

    # ---- initialize ----
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "serverInfo": {
                    "name": "Multi-User Google Sheets MCP",
                    "version": "0.1.0",
                }
            },
        }

    # ---- tools/list ----
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": id_,
            "result": {
                "tools": [
                    {
                        "name": "sheets.read_range",
                        "description": "Read values from a Google Sheet range",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "spreadsheet_id": {"type": "string"},
                                "range": {"type": "string"},
                            },
                            "required": ["spreadsheet_id", "range"],
                        },
                    },
                    {
                        "name": "sheets.write_range",
                        "description": "Write values to a Google Sheet range",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "spreadsheet_id": {"type": "string"},
                                "range": {"type": "string"},
                                "values": {
                                    "type": "array",
                                    "items": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                            "required": ["spreadsheet_id", "range", "values"],
                        },
                    },
                    {
                        "name": "sheets.append_row",
                        "description": "Append a new row to a Google Sheet",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "spreadsheet_id": {"type": "string"},
                                "range": {"type": "string"},
                                "values": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["spreadsheet_id", "range", "values"],
                        },
                    },
                ]
            },
        }

    # ---- tools/call ----
    if method == "tools/call":
        tool = payload["params"]["name"]
        args = payload["params"].get("arguments", {})

        if tool == "sheets.read_range":
            res = sheets_read_range(
                user_id,
                args["spreadsheet_id"],
                args["range"],
            )
            if res == "AUTH_REQUIRED":
                return auth_error(id_, user_id)
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "result": {"content": [{"type": "json", "json": res}]},
            }

        if tool == "sheets.write_range":
            res = sheets_write_range(
                user_id,
                args["spreadsheet_id"],
                args["range"],
                args["values"],
            )
            if res == "AUTH_REQUIRED":
                return auth_error(id_, user_id)
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "result": {"content": [{"type": "text", "text": "✅ Sheet updated"}]},
            }

        if tool == "sheets.append_row":
            res = sheets_append_row(
                user_id,
                args["spreadsheet_id"],
                args["range"],
                args["values"],
            )
            if res == "AUTH_REQUIRED":
                return auth_error(id_, user_id)
            return {
                "jsonrpc": "2.0",
                "id": id_,
                "result": {"content": [{"type": "text", "text": "➕ Row appended"}]},
            }

    return JSONResponse(
        status_code=400,
        content={
            "jsonrpc": "2.0",
            "id": id_,
            "error": {"code": -32601, "message": "Method not found"},
        },
    )


@app.get("/")
def health():
    return {"status": "Sheets MCP running"}
