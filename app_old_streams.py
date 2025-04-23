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
import random
# Configuration
CLIENT_SECRETS_FILE = "client_secrets_1.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.force-ssl"]
API_SERVICE_NAME = "youtube"
API_VERSION = "v3"
TOKEN_FILE = "token.json"
##########################################
random_start = random.randint(0, 1560)
# Livestream processing configuration
API_KEY = ""
CHANNEL_ID = ""
PROCESSING_DIR = "./timelapse"  # Changed from DOWNLOAD_DIR to PROCESSING_DIR
AUDIO_TRACK = "lofi.mp3"  # Pre-downloaded royalty-free track
FFMPEG_CMD = 'ffmpeg -i "{input_file}" -ss {random_start} -i lofi.mp3 -r 60 -filter:v "setpts=0.00234*PTS" -map 0:v -map 1:a -shortest  -vcodec libx264 -acodec aac "{output_file}"'
MAX_RETRIES = 3
RETRY_DELAY = 10  # seconds to wait between retries
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

def get_livestream_info_for_urls(youtube, urls):
    """Get full livestream info for a list of URLs"""
    video_ids = []
    for url in urls:
        # Extract video ID from URL
        if "v=" in url:
            video_id = url.split("v=")[1].split("&")[0]
            video_ids.append(video_id)
    
    livestreams = []
    for i in range(0, len(video_ids), 50):  # Process in batches of 50
        batch_ids = video_ids[i:i+50]
        try:
            videos_response = youtube.videos().list(
                id=",".join(batch_ids),
                part="liveStreamingDetails,snippet,status"
            ).execute()
            
            for video in videos_response.get("items", []):
                if "liveStreamingDetails" in video and "actualEndTime" in video["liveStreamingDetails"]:
                    livestreams.append({
                        "id": video["id"],
                        "url": f"https://www.youtube.com/watch?v={video['id']}",
                        "title": video["snippet"]["title"],
                        "publishedAt": video["snippet"]["publishedAt"],
                        "endTime": video["liveStreamingDetails"]["actualEndTime"]
                    })
        except HttpError as e:
            logger.error(f"YouTube API Error while fetching video info: {e}")
            continue
    
    return livestreams

def get_livestreams_to_process(youtube):
    """Get livestreams to process, prioritizing new_urls.txt if it exists and isn't empty"""
    processed_urls = load_processed_urls()
    
    # First check if new_urls.txt exists and has unprocessed URLs
    if os.path.exists(NEW_URLS_FILE) and os.path.getsize(NEW_URLS_FILE) > 0:
        logger.info(f"Found existing {NEW_URLS_FILE}, checking for unprocessed URLs")
        with open(NEW_URLS_FILE, "r") as f:
            urls = [line.strip() for line in f if line.strip()]
        
        unprocessed_urls = [url for url in urls if url not in processed_urls]
        
        if unprocessed_urls:
            logger.info(f"Found {len(unprocessed_urls)} unprocessed URLs in {NEW_URLS_FILE}")
            # We need to get full livestream info for these URLs
            return get_livestream_info_for_urls(youtube, unprocessed_urls)
        else:
            logger.info(f"All URLs in {NEW_URLS_FILE} have already been processed")
    
    # If new_urls.txt is empty or doesn't exist, fetch fresh livestreams
    logger.info("No unprocessed URLs in new_urls.txt, fetching fresh livestreams")
    return get_all_completed_livestreams(youtube)


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
    """Download video using yt-dlp with retry logic"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'outtmpl': os.path.join(PROCESSING_DIR, filename),
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
    }

    for attempt in range(MAX_RETRIES):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return True
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"Download attempt {attempt + 1} failed, retrying in {RETRY_DELAY} seconds... Error: {e}")
                time.sleep(RETRY_DELAY)
                continue
            logger.error(f"Download failed after {MAX_RETRIES} attempts: {e}")
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
    """Upload video to YouTube with retry logic"""
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return None

    if title is None:
        title = os.path.splitext(os.path.basename(file_path))[0]

    if description is None:
        description = read_description()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["clouds timelapse","weather timelapse","relaxing sky","4K nature", "ASMR clouds"],
            #"tags": ["livestream", "timelapse"],
            "categoryId": "22"  # People & Blogs
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False
        }
    }

    for attempt in range(MAX_RETRIES):
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

            logger.info(f"Upload attempt {attempt + 1} for {file_path}...")
            response = request.execute()
            logger.info(f"Upload successful! Video ID: {response.get('id')}")
            return response
        except HttpError as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"YouTube API error during upload attempt {attempt + 1}, retrying in {RETRY_DELAY} seconds... Error: {e}")
                time.sleep(RETRY_DELAY)
                continue
            logger.error(f"YouTube API error during upload: {e}")
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                logger.warning(f"Upload error attempt {attempt + 1}, retrying in {RETRY_DELAY} seconds... Error: {e}")
                time.sleep(RETRY_DELAY)
                continue
            logger.error(f"Upload error: {e}")
            return None
def update_video_description(youtube, video_id, timelapse_url):
    """Update the original video's description to include the timelapse link"""
    try:
        # Get the current video details
        video_response = youtube.videos().list(
            id=video_id,
            part="snippet"
        ).execute()

        if not video_response.get("items"):
            logger.error("Original video not found")
            return False

        video = video_response["items"][0]
        snippet = video["snippet"]
        current_description = snippet.get("description", "")
        
        # Check if the timelapse link is already in the description
        if timelapse_url in current_description:
            logger.info("Timelapse link already exists in original video description")
            return True
            
        # Add the timelapse link to the description
        separator = "\n\n" if current_description else ""
        new_description = f"{current_description}{separator}Timelapse version: {timelapse_url}"
        
        # Update the video
        snippet["description"] = new_description
        update_response = youtube.videos().update(
            part="snippet",
            body={
                "id": video_id,
                "snippet": snippet
            }
        ).execute()
        
        logger.info(f"Successfully updated original video description with timelapse link")
        return True
        
    except HttpError as e:
        logger.error(f"YouTube API error updating description: {e}")
        return False
    except Exception as e:
        logger.error(f"Error updating video description: {e}")
        return False
def process_livestream(livestream):
    """Process a single livestream: download, timelapse, upload, cleanup with retry logic"""
    logger.info(f"Processing livestream: {livestream['title']}")
    logger.info(f"Stream ended at: {livestream['endTime']}")

    os.makedirs(PROCESSING_DIR, exist_ok=True)

    input_filename = f"input_{livestream['id']}.mp4"
    input_path = os.path.join(PROCESSING_DIR, input_filename)
    timestamp = datetime.strptime(livestream["publishedAt"], "%Y-%m-%dT%H:%M:%SZ").strftime("%Y%m%d_%H%M%S")
    output_filename = f"livestream_{timestamp}.mp4"
    output_path = os.path.join(PROCESSING_DIR, output_filename)

    # Download with retries
    logger.info("Downloading video...")
    if not download_video(livestream["id"], input_filename):
        logger.error("Failed to download video after multiple attempts")
        return False

    # Process with FFmpeg
    logger.info("Processing with FFmpeg...")
    try:
        os.system(FFMPEG_CMD.format(input_file=input_path, random_start=random_start, output_file=output_path))
        if not os.path.exists(output_path):
            raise Exception("FFmpeg processing failed - output file not created")
    except Exception as e:
        logger.error(f"Error during FFmpeg processing: {e}")
        try:
            os.remove(input_path)
            logger.info(f"Cleaned up downloaded file: {input_filename}")
        except OSError:
            pass
        return False

    # Clean up input file
    try:
        os.remove(input_path)
        logger.info(f"Deleted downloaded video: {input_filename}")
    except OSError as e:
        logger.error(f"Error removing temporary file: {e}")

    # Upload with retries
    logger.info("Preparing to upload video...")
    youtube_upload = get_authenticated_service()
    #video_title = f"Timelapse: {livestream['title']}"
    video_title = generate_video_title(livestream["title"], livestream["endTime"])
    #description = f"Timelapse created from livestream on {livestream['endTime']}\n\nOriginal stream: {livestream['url']}"
    description = generate_description(livestream)
    #upload_success = False
    response = upload_video(youtube_upload, output_path, video_title, description)
    if response:
        logger.info("Video uploaded successfully!")
        uploaded_video_url = f"https://www.youtube.com/watch?v={response.get('id')}"
        youtube = setup_youtube_client()
        if youtube:
            update_video_description(youtube_upload, livestream["id"], uploaded_video_url)
        save_processed_url(livestream["url"])  # Only mark as processed after successful upload
        remove_from_new_urls(livestream["url"])  # Remove from new_urls.txt
        upload_success = True
    else:
        logger.error("Video upload failed after multiple attempts. Keeping local timelapse file.")

    # Clean up output file
    try:
        os.remove(output_path)
        logger.info(f"Deleted local timelapse file: {output_filename}")
    except OSError as e:
        logger.error(f"Error deleting timelapse file: {e}")

    return upload_success

def generate_description(livestream):
    """Auto-generate an SEO-friendly description"""
    end_time = datetime.strptime(livestream["endTime"], "%Y-%m-%dT%H:%M:%SZ")
    location = "Unknown"  # You can hardcode or detect from title
    
    return f"""🌤️ {livestream['title']} (Timelapse Version)

Watch the sky transform at {60}x speed! Filmed on {end_time.strftime('%B %d, %Y')} in {location}.

► Original Livestream: {livestream['url']}
► Subscribe for daily cloud timelapses: https://www.youtube.com/@streamraspberrypi69420

This timelapse is perfect for:
- Relaxation & stress relief
- Background visuals for work/study
- Weather enthusiasts
- ASMR/sleep aid

Equipment: raspberrypi camera v2
Location: italy🇮🇹 

#Clouds #Timelapse #Relaxation #Weather #Storm #Sky
"""
def generate_tags(livestream_title):
    """Generate relevant tags based on content"""
    base_tags = [
        "clouds timelapse",
        "weather timelapse",
        "relaxing sky",
        "4K nature",
        "ASMR clouds"
    ]
    
    # Add context-specific tags
    if "storm" in livestream_title.lower():
        base_tags.extend(["storm clouds", "thunderstorm", "lightning"])
    elif "sunset" in livestream_title.lower():
        base_tags.extend(["sunset sky", "golden hour", "dusk"])
    
    return base_tags

def add_to_playlist(youtube, video_id, playlist_name="Cloud Timelapses"):
    """Add video to a playlist (create if missing)"""
    try:
        # Get or create playlist
        playlists = youtube.playlists().list(
            part="snippet",
            mine=True,
            maxResults=50
        ).execute()
        
        playlist_id = None
        for pl in playlists.get("items", []):
            if pl["snippet"]["title"] == playlist_name:
                playlist_id = pl["id"]
                break
        
        if not playlist_id:
            # Create new playlist
            new_playlist = youtube.playlists().insert(
                part="snippet,status",
                body={
                    "snippet": {
                        "title": playlist_name,
                        "description": f"Auto-generated {playlist_name} collection"
                    },
                    "status": {
                        "privacyStatus": "public"
                    }
                }
            ).execute()
            playlist_id = new_playlist["id"]
        
        # Add video
        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id
                    }
                }
            }
        ).execute()
    except Exception as e:
        logger.error(f"Playlist error: {e}")
		
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
    logger.info(f"Checking for unprocessed livestreams at {current_time}...")

    try:
        youtube = setup_youtube_client()
        if youtube is None:
            logger.error("Failed to initialize YouTube client")
            return

        livestreams = get_livestreams_to_process(youtube)
        if not livestreams:
            logger.info("No unprocessed livestreams found")
            return

        logger.info(f"Found {len(livestreams)} unprocessed livestream(s)")

        for livestream in livestreams:
            try:
                success = process_livestream(livestream)
                if not success:
                    logger.error(f"Failed to process livestream {livestream['url']} after retries")
            except Exception as e:
                logger.error(f"Error processing livestream {livestream['url']}: {e}")
                continue

    except Exception as e:
        logger.error(f"Unexpected error in process_all_videos: {e}")
#def process_all_videos():
#    """Process all unprocessed livestreams from the channel"""
#    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#    logger.info(f"Checking for all unprocessed livestreams at {current_time}...")
#
#    try:
#        youtube = setup_youtube_client()
#        if youtube is None:
#            logger.error("Failed to initialize YouTube client")
#            return
#
#        livestreams = get_all_completed_livestreams(youtube)
#        if not livestreams:
#            logger.info("No unprocessed livestreams found")
#            return
#
#        logger.info(f"Found {len(livestreams)} unprocessed livestream(s)")
#
#        for livestream in livestreams:
#            try:
#                success = process_livestream(livestream)
#                if not success:
#                    logger.error(f"Failed to process livestream {livestream['url']} after #retries")
#            except Exception as e:
#                logger.error(f"Error processing livestream {livestream['url']}: {e}")
#                continue
#
#    except Exception as e:
#        logger.error(f"Unexpected error in process_all_videos: {e}")
def generate_video_title(original_title, end_time):
    """Generate optimized title based on content type"""
    # Extract keywords from original stream title
    keywords = {
        "storm": ["Storm", "Thunder", "Lightning", "Rolling Clouds"],
        "calm": ["Relaxing", "Peaceful", "Calm", "Soothing"],
        "sunset": ["Sunset", "Golden Hour", "Dusk"],
        "sunrise": ["Sunrise", "Dawn", "Morning Sky"]
    }
    
    # Detect content type (simplified - you can improve this)
    content_type = "calm"  # default
    if any(word.lower() in original_title.lower() for word in keywords["storm"]):
        content_type = "storm"
    elif "sunset" in original_title.lower():
        content_type = "sunset"
    elif "sunrise" in original_title.lower():
        content_type = "sunrise"
    
    # Template selection
    templates = {
        "storm": [
            "⚡ {speed}x Storm Timelapse - {date}",
            "Thunder Clouds Rolling In - {speed}x Timelapse"
        ],
        "calm": [
            "☁️ {speed}x Relaxing Cloud Timelapse ({duration})",
            "Calming Sky Motion - {speed}x Timelapse"
        ],
        "sunset": [
            "🌅 Sunset Sky Timelapse - {speed}x Speed",
            "Golden Hour Clouds - {date}"
        ],
        "sunrise": [
            "🌄 Sunrise Timelapse - {speed}x Speed",
            "Morning Sky Transformation - {date}"
        ]
    }
    
    # Fill template
    import random
    chosen_template = random.choice(templates[content_type])
    date_str = datetime.strptime(end_time, "%Y-%m-%dT%H:%M:%SZ").strftime("%b %d, %Y")
    
    return chosen_template.format(
        speed="60x",  # Adjust based on your FFMPEG speed
        date=date_str #,
        #duration="1 Hour"  # Can be dynamic
    )
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
