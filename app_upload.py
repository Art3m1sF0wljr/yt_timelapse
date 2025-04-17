import os
import argparse
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

# Configuration
CLIENT_SECRETS_FILE = "client_secrets.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"
TOKEN_FILE = "token.json"

def get_authenticated_service():
    """Authenticate and return the YouTube service, caching credentials"""
    creds = None

    # Load existing credentials if available
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # If credentials are invalid or expired, refresh them
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0, authorization_prompt_message="")

        # Save the credentials for next time
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())

    return build(API_SERVICE_NAME, API_VERSION, credentials=creds)

def read_description():
    """Read description from description.txt or return default"""
    try:
        with open("description.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Uploaded video"
    except Exception as e:
        print(f"Error reading description: {e}")
        return "Uploaded video"

def upload_video(youtube, file_path, title=None):
    """Upload video to YouTube"""
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        return False

    if title is None:
        title = os.path.splitext(os.path.basename(file_path))[0]

    description = read_description()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["uploaded video"],
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "public",  # Change to "private" or "unlisted" if needed
            "selfDeclaredMadeForKids": False
        }
    }

    try:
        media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
        request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media
        )

        print(f"Uploading {file_path}...")
        response = request.execute()
        print(f"Upload successful! Video ID: {response.get('id')}")
        return True
    except Exception as e:
        print(f"An error occurred during upload: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description='Upload a video to YouTube')
    parser.add_argument('video_file', help='Path to the video file to upload')
    parser.add_argument('--title', help='Custom title for the video (optional)')
    args = parser.parse_args()

    if not os.path.exists(args.video_file):
        print(f"Error: File not found: {args.video_file}")
        return

    youtube = get_authenticated_service()
    if upload_video(youtube, args.video_file, args.title):
        print("Upload completed successfully!")
    else:
        print("Upload failed.")

if __name__ == "__main__":
    main()
