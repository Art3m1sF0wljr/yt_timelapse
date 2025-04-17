import os
import time
import argparse
import logging
from datetime import datetime, timedelta
import googleapiclient.discovery
import schedule
import yt_dlp
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

# Configuration
CLIENT_SECRETS_FILE = "client_secrets.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"
TOKEN_FILE = "token.json"

# Livestream processing configuration
API_KEY = ""
CHANNEL_ID = ""
PROCESSING_DIR = "./timelapse"  # Changed from DOWNLOAD_DIR to PROCESSING_DIR
FFMPEG_CMD = 'ffmpeg -i "{input_file}" -r 60 -filter:v "setpts=0.00234*PTS" -vcodec libx264 -an "{output_file}"'
MAX_RETRIES = 3
URLS_FILE = "urls.txt"  # File to store processed URLs
NEW_URLS_FILE = "new_urls.txt"  # File to store all livestream URLs from channel

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('youtube_processor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def get_authenticated_service():
    """Authenticate and return the YouTube service, caching credentials"""
    creds = None

    # Load existing credentials if available
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    # If credentials are invalid or expired, refresh them
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                logger.error(f"Failed to refresh token: {e}")
                os.remove(TOKEN_FILE)  # Remove invalid token
                return get_authenticated_service()  # Retry with new auth flow
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0, authorization_prompt_message="")

        # Save the credentials for next time
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())

    return build(API_SERVICE_NAME, API_VERSION, credentials=creds)

def setup_youtube_client():
    """Setup YouTube client for public API access (no OAuth)"""
    try:
        return googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)
    except Exception as e:
        logger.error(f"Failed to setup YouTube client: {e}")
        return None

def read_description():
    """Read description from description.txt or return default"""
    try:
        with open("description.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "Processed livestream timelapse"
    except Exception as e:
        logger.error(f"Error reading description: {e}")
        return "Processed livestream timelapse"

def get_all_completed_livestreams(youtube):
    """Find all completed livestreams from channel that haven't been processed yet"""
    try:
        # First get the uploads playlist ID
        channels_response = youtube.channels().list(
            id=CHANNEL_ID,
            part="contentDetails"
        ).execute()

        if not channels_response.get("items"):
            logger.info("Channel not found or no content details available")
            return []

        uploads_playlist_id = channels_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        processed_urls = load_processed_urls()
        all_livestreams = []
        next_page_token = None

        while True:
            # Get videos from the uploads playlist
            playlist_items = youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part="snippet",
                maxResults=50,
                pageToken=next_page_token
            ).execute()

            if not playlist_items.get("items"):
                break

            video_ids = [item["snippet"]["resourceId"]["videoId"] for item in playlist_items["items"]]

            # Batch process videos to check for livestreams
            for i in range(0, len(video_ids), 50):  # YouTube API allows max 50 IDs per request
                batch_ids = video_ids[i:i+50]
                videos_response = youtube.videos().list(
                    id=",".join(batch_ids),
                    part="liveStreamingDetails,snippet,status"
                ).execute()

                for video in videos_response.get("items", []):
                    if "liveStreamingDetails" in video and "actualEndTime" in video["liveStreamingDetails"]:
                        video_url = f"https://www.youtube.com/watch?v={video['id']}"
                        if video_url not in processed_urls:
                            all_livestreams.append({
                                "id": video["id"],
                                "url": video_url,
                                "title": video["snippet"]["title"],
                                "publishedAt": video["snippet"]["publishedAt"],
                                "endTime": video["liveStreamingDetails"]["actualEndTime"]
                            })

            next_page_token = playlist_items.get("nextPageToken")
            if not next_page_token:
                break

        # Sort by published date (oldest first)
        all_livestreams.sort(key=lambda x: x["publishedAt"])

        # Save all livestream URLs to new_urls.txt
        with open(NEW_URLS_FILE, "w") as f:
            for stream in all_livestreams:
                f.write(stream["url"] + "\n")

        return all_livestreams

    except HttpError as e:
        logger.error(f"YouTube API Error: {e}")
        return []
    except Exception as e:
        logger.error(f"Unexpected error in get_all_completed_livestreams: {e}")
        return []

def download_video(video_id, filename):
    """Download video using yt-dlp"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'outtmpl': os.path.join(PROCESSING_DIR, filename),
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return True
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return False

def load_processed_urls():
    """Load set of already processed URLs"""
    try:
        with open(URLS_FILE, "r") as f:
            return set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        return set()
    except Exception as e:
        logger.error(f"Error loading processed URLs: {e}")
        return set()

def save_processed_url(url):
    """Save URL to prevent duplicate processing"""
    try:
        with open(URLS_FILE, "a") as f:
            f.write(url + "\n")
    except Exception as e:
        logger.error(f"Error saving URL: {e}")

def upload_video(youtube, file_path, title=None, description=None, privacy="public"):
    """Upload video to YouTube"""
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return False

    if title is None:
        title = os.path.splitext(os.path.basename(file_path))[0]

    if description is None:
        description = read_description()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["livestream", "timelapse"],
            "categoryId": "22"  # People & Blogs
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False
        }
    }

    try:
        media = MediaFileUpload(
            file_path,
            chunksize=-1,
            resumable=True
        )

        request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media
        )

        logger.info(f"Uploading {file_path}...")
        response = request.execute()
        logger.info(f"Upload successful! Video ID: {response.get('id')}")
        return True
    except HttpError as e:
        logger.error(f"YouTube API error during upload: {e}")
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return False

def process_livestream(livestream):
    """Process a single livestream: download, timelapse, upload, cleanup"""
    logger.info(f"Processing livestream: {livestream['title']}")
    logger.info(f"Stream ended at: {livestream['endTime']}")

    # Save URL before processing to avoid duplicates
    save_processed_url(livestream["url"])
    logger.info(f"Saved URL to {URLS_FILE}")

    os.makedirs(PROCESSING_DIR, exist_ok=True)

    input_filename = f"input_{livestream['id']}.mp4"
    input_path = os.path.join(PROCESSING_DIR, input_filename)
    timestamp = datetime.strptime(livestream["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").strftime("%Y%m%d_%H%M%S")
    output_filename = f"livestream_{timestamp}.mp4"
    output_path = os.path.join(PROCESSING_DIR, output_filename)

    logger.info("Downloading video...")
    if not download_video(livestream["id"], input_filename):
        logger.error("Failed to download video")
        return False

    logger.info("Processing with FFmpeg...")
    os.system(FFMPEG_CMD.format(input_file=input_path, output_file=output_path))

    try:
        os.remove(input_path)
        logger.info(f"Deleted downloaded video: {input_filename}")
    except OSError as e:
        logger.error(f"Error removing temporary file: {e}")

    # Upload the processed video
    logger.info("Preparing to upload video...")
    youtube_upload = get_authenticated_service()
    video_title = f"Timelapse: {livestream['title']}"
    description = f"Timelapse created from livestream on {livestream['endTime']}\n\nOriginal stream: {livestream['url']}"

    upload_success = False
    if upload_video(youtube_upload, output_path, video_title, description):
        logger.info("Video uploaded successfully!")
        save_processed_url(livestream["url"])  # Add to urls.txt
        remove_from_new_urls(livestream["url"])  # Remove from new_urls.txt
        upload_success = True
    else:
        logger.error("Video upload failed. Keeping local timelapse file.")

    try:
        os.remove(output_path)
        logger.info(f"Deleted local timelapse file: {output_filename}")
    except OSError as e:
        logger.error(f"Error deleting timelapse file: {e}")

    return upload_success

def remove_from_new_urls(url):
    """Remove a URL from new_urls.txt if it exists"""
    try:
        # Read all lines except the one matching our URL
        with open(NEW_URLS_FILE, "r") as f:
            lines = [line.strip() for line in f if line.strip() != url]
        
        # Write back the remaining lines
        with open(NEW_URLS_FILE, "w") as f:
            f.write("\n".join(lines) + "\n")
            
        logger.info(f"Removed {url} from {NEW_URLS_FILE}")
    except Exception as e:
        logger.error(f"Error updating {NEW_URLS_FILE}: {e}")

def process_all_videos():
    """Process all unprocessed livestreams from the channel"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Checking for all unprocessed livestreams at {current_time}...")

    try:
        youtube = setup_youtube_client()
        if youtube is None:
            logger.error("Failed to initialize YouTube client")
            return

        livestreams = get_all_completed_livestreams(youtube)
        if not livestreams:
            logger.info("No unprocessed livestreams found")
            return

        logger.info(f"Found {len(livestreams)} unprocessed livestream(s)")

        for livestream in livestreams:
            try:
                process_livestream(livestream)
            except Exception as e:
                logger.error(f"Error processing livestream {livestream['url']}: {e}")
                continue

    except Exception as e:
        logger.error(f"Unexpected error in process_all_videos: {e}")

def main():
    parser = argparse.ArgumentParser(description='YouTube Livestream Processor')
    parser.add_argument('--run-once', action='store_true', help='Run once and exit (default behavior)')
    args = parser.parse_args()

    logger.info("YouTube Livestream Processor started")
    logger.info(f"Processing directory: {os.path.abspath(PROCESSING_DIR)}")
    logger.info(f"Processed URLs stored in: {os.path.abspath(URLS_FILE)}")
    logger.info(f"All livestream URLs stored in: {os.path.abspath(NEW_URLS_FILE)}")

    process_all_videos()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Script stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
