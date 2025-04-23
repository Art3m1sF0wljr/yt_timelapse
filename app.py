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
#####################################
random_start = random.randint(0, 1560)
# Livestream processing configuration
API_KEY = ""
CHANNEL_ID = ""
DOWNLOAD_DIR = "./downloads"
AUDIO_TRACK = "lofi.mp3"
FFMPEG_CMD = 'ffmpeg -i "{input_file}" -ss {random_start} -i lofi.mp3 -r 60 -filter:v "setpts=0.00234*PTS" -map 0:v -map 1:a -shortest -vcodec libx264 -acodec aac "{output_file}"'
CHECK_INTERVAL = 40  # minutes
MAX_RETRIES = 3
URLS_FILE = "urls.txt"  # File to store processed URLs

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
    if not creds or not creds.valid or not creds.has_scopes(SCOPES):
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                logger.error(f"Failed to refresh token: {e}")
                os.remove(TOKEN_FILE)  # Remove invalid token
                return get_authenticated_service()  # Retry with new auth flow
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            #creds = flow.run_local_server(port=0, authorization_prompt_message="")
	    #flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
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
def get_latest_completed_livestream(youtube):
    """Find the latest started livestream that ended after 18/04/2025 with reduced API calls"""
    try:
        logger.info("Starting optimized livestream search...")
        processed_urls = load_processed_urls()

        # First get the uploads playlist ID (1 API call)
        channels_response = youtube.channels().list(
            id=CHANNEL_ID,
            part="contentDetails"
        ).execute()

        if not channels_response.get("items"):
            logger.error("Channel not found or no content details available")
            return None

        uploads_playlist_id = channels_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        logger.info(f"Found uploads playlist ID: {uploads_playlist_id}")

        # Get only the most recent videos (limit to 100)
        max_videos_to_check = 100
        videos_checked = 0
        latest_livestream = None
        latest_start_time = None
        next_page_token = None
        min_start_date = datetime(2025, 4, 23, 0, 0, 0).isoformat() + "Z"

        while videos_checked < max_videos_to_check:
            # Get batch of videos (1 API call per batch)
            playlist_response = youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part="snippet",
                maxResults=min(50, max_videos_to_check - videos_checked),
                pageToken=next_page_token
            ).execute()

            if not playlist_response.get("items"):
                break

            # Collect video IDs for batch processing
            video_ids = []
            video_info = {}
            for item in playlist_response["items"]:
                video_id = item["snippet"]["resourceId"]["videoId"]
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                
                if video_url in processed_urls:
                    continue
                
                video_ids.append(video_id)
                video_info[video_id] = {
                    "url": video_url,
                    "title": item["snippet"]["title"],
                    "publishedAt": item["snippet"]["publishedAt"]
                }
                videos_checked += 1

            if not video_ids:
                next_page_token = playlist_response.get("nextPageToken")
                if not next_page_token:
                    break
                continue

            # Batch get video details (1 API call per 50 videos)
            for i in range(0, len(video_ids), 50):
                batch_ids = video_ids[i:i+50]
                videos_response = youtube.videos().list(
                    id=",".join(batch_ids),
                    part="liveStreamingDetails,snippet",
                    fields="items(id,snippet(title),liveStreamingDetails(actualStartTime,actualEndTime))"
                ).execute()

                if not videos_response.get("items"):
                    continue

                for video in videos_response["items"]:
                    # Must be a completed livestream
                    if ("liveStreamingDetails" not in video or 
                        "actualStartTime" not in video["liveStreamingDetails"] or 
                        "actualEndTime" not in video["liveStreamingDetails"]):
                        continue

                    start_time = video["liveStreamingDetails"]["actualStartTime"]
                    if start_time < min_start_date:
                        continue

                    # If this is the most recent started livestream we've found so far
                    if latest_start_time is None or start_time > latest_start_time:
                        latest_start_time = start_time
                        latest_livestream = {
                            "id": video["id"],
                            "url": video_info[video["id"]]["url"],
                            "title": video["snippet"]["title"],
                            "startTime": start_time,
                            "endTime": video["liveStreamingDetails"]["actualEndTime"]
                        }
                        logger.info(f"New candidate found: {video['snippet']['title']} (started: {start_time})")

            next_page_token = playlist_response.get("nextPageToken")
            if not next_page_token or videos_checked >= max_videos_to_check:
                break

        if latest_livestream:
            logger.info(f"Selected most recently started livestream: {latest_livestream['title']}")
            logger.info(f"Started: {latest_livestream['startTime']}, Ended: {latest_livestream['endTime']}")
            return latest_livestream

        logger.info("No unprocessed completed livestreams found that started after April 18, 2025")
        return None

    except HttpError as e:
        logger.error(f"YouTube API Error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_latest_completed_livestream: {e}")
        return None
def ole_2_get_latest_completed_livestream(youtube):
    """Find the latest started (not just newest published) livestream that hasn't been processed"""
    try:
        logger.info("Starting livestream search...")
        processed_urls = load_processed_urls()

        # First get the uploads playlist ID
        channels_response = youtube.channels().list(
            id=CHANNEL_ID,
            part="contentDetails"
        ).execute()

        if not channels_response.get("items"):
            logger.error("Channel not found or no content details available")
            return None

        uploads_playlist_id = channels_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        logger.info(f"Found uploads playlist ID: {uploads_playlist_id}")

        # Get all available videos from the uploads playlist
        playlist_items = []
        next_page_token = None

        while True:
            playlist_response = youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part="snippet",
                maxResults=50,
                pageToken=next_page_token
            ).execute()

            playlist_items.extend(playlist_response["items"])
            next_page_token = playlist_response.get("nextPageToken")

            if not next_page_token:
                break

        logger.info(f"Found {len(playlist_items)} total videos in playlist")

        # We'll track the most recent started livestream that meets our date criteria
        latest_livestream = None
        latest_start_time = None
        
        # Define the minimum start date (April 18, 2025, 00:00:00 UTC)
        min_start_date = datetime(2025, 4, 18, 0, 0, 0).isoformat() + "Z"

        for item in playlist_items:
            video_id = item["snippet"]["resourceId"]["videoId"]
            video_url = f"https://www.youtube.com/watch?v={video_id}"

            # Skip already processed videos
            if video_url in processed_urls:
                continue

            video_response = youtube.videos().list(
                id=video_id,
                part="liveStreamingDetails,snippet",
                fields="items(id,snippet(title),liveStreamingDetails(actualStartTime,actualEndTime))"
            ).execute()

            if not video_response.get("items"):
                continue

            video = video_response["items"][0]

            # Must be a completed livestream
            if "liveStreamingDetails" not in video:
                continue
            if "actualStartTime" not in video["liveStreamingDetails"]:
                continue
            if "actualEndTime" not in video["liveStreamingDetails"]:
                continue

            start_time = video["liveStreamingDetails"]["actualStartTime"]
            
            # Skip if the livestream started before our minimum date
            if start_time < min_start_date:
                #logger.info(f"Skipping livestream started before April 18, 2025: {start_time}")
                continue

            # If this is the most recent started livestream we've found so far
            if latest_start_time is None or start_time > latest_start_time:
                latest_start_time = start_time
                latest_livestream = {
                    "id": video_id,
                    "url": video_url,
                    "title": video["snippet"]["title"],
                    "startTime": start_time,
                    "endTime": video["liveStreamingDetails"]["actualEndTime"]
                }
                logger.info(f"New candidate found: {video['snippet']['title']} (started: {start_time})")

        if latest_livestream:
            logger.info(f"Selected most recently started livestream: {latest_livestream['title']}")
            logger.info(f"Started: {latest_livestream['startTime']}, Ended: {latest_livestream['endTime']}")
            return latest_livestream

        logger.info("No unprocessed completed livestreams found that started after April 18, 2025")
        return None

    except HttpError as e:
        logger.error(f"YouTube API Error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_latest_completed_livestream: {e}")
        return None

def old_get_latest_completed_livestream(youtube):
    """Find the latest started (not just newest published) livestream that hasn't been processed"""
    try:
        logger.info("Starting livestream search...")
        processed_urls = load_processed_urls()
        
        # First get the uploads playlist ID
        channels_response = youtube.channels().list(
            id=CHANNEL_ID,
            part="contentDetails"
        ).execute()

        if not channels_response.get("items"):
            logger.error("Channel not found or no content details available")
            return None

        uploads_playlist_id = channels_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        logger.info(f"Found uploads playlist ID: {uploads_playlist_id}")

        # Get all available videos from the uploads playlist
        playlist_items = []
        next_page_token = None
        
        while True:
            playlist_response = youtube.playlistItems().list(
                playlistId=uploads_playlist_id,
                part="snippet",
                maxResults=50,
                pageToken=next_page_token
            ).execute()
            
            playlist_items.extend(playlist_response["items"])
            next_page_token = playlist_response.get("nextPageToken")
            
            if not next_page_token:
                break

        logger.info(f"Found {len(playlist_items)} total videos in playlist")
        
        # We'll track the most recent started livestream
        latest_livestream = None
        latest_start_time = None
        
        for item in playlist_items:
            video_id = item["snippet"]["resourceId"]["videoId"]
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            
            # Skip already processed videos
            if video_url in processed_urls:
                continue
                
            video_response = youtube.videos().list(
                id=video_id,
                part="liveStreamingDetails,snippet",
                fields="items(id,snippet(title),liveStreamingDetails(actualStartTime,actualEndTime))"
            ).execute()

            if not video_response.get("items"):
                continue

            video = video_response["items"][0]
            
            # Must be a completed livestream
            if "liveStreamingDetails" not in video:
                continue
            if "actualStartTime" not in video["liveStreamingDetails"]:
                continue
            if "actualEndTime" not in video["liveStreamingDetails"]:
                continue
                
            start_time = video["liveStreamingDetails"]["actualStartTime"]
            
            # If this is the most recent started livestream we've found so far
            if latest_start_time is None or start_time > latest_start_time:
                latest_start_time = start_time
                latest_livestream = {
                    "id": video_id,
                    "url": video_url,
                    "title": video["snippet"]["title"],
                    "startTime": start_time,
                    "endTime": video["liveStreamingDetails"]["actualEndTime"]
                }
                logger.info(f"New candidate found: {video['snippet']['title']} (started: {start_time})")

        if latest_livestream:
            logger.info(f"Selected most recently started livestream: {latest_livestream['title']}")
            logger.info(f"Started: {latest_livestream['startTime']}, Ended: {latest_livestream['endTime']}")
            return latest_livestream
            
        logger.info("No unprocessed completed livestreams found")
        return None

    except HttpError as e:
        logger.error(f"YouTube API Error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_latest_completed_livestream: {e}")
        return None

def old_2_get_latest_completed_livestream(youtube):
    """Find the latest completed livestream from channel"""
    try:
        # First get the uploads playlist ID
        channels_response = youtube.channels().list(
            id=CHANNEL_ID,
            part="contentDetails"
        ).execute()

        if not channels_response.get("items"):
            logger.info("Channel not found or no content details available")
            return None

        uploads_playlist_id = channels_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

        # Get recent videos from the uploads playlist
        playlist_items = youtube.playlistItems().list(
            playlistId=uploads_playlist_id,
            part="snippet",
            maxResults=500
        ).execute()

        if not playlist_items.get("items"):
            logger.info("No videos found in uploads playlist")
            return None

        for item in playlist_items["items"]:
            video_id = item["snippet"]["resourceId"]["videoId"]
            video_response = youtube.videos().list(
                id=video_id,
                part="liveStreamingDetails,snippet,status"
            ).execute()

            if not video_response.get("items"):
                continue

            video = video_response["items"][0]

            if "liveStreamingDetails" in video and "actualEndTime" in video["liveStreamingDetails"]:
                return {
                    "id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": video["snippet"]["title"],
                    "publishedAt": video["snippet"]["publishedAt"],
                    "endTime": video["liveStreamingDetails"]["actualEndTime"]
                }
        return None

    except HttpError as e:
        logger.error(f"YouTube API Error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_latest_completed_livestream: {e}")
        return None

def old_1_get_latest_completed_livestream(youtube):
    """Find the latest completed livestream from channel with improved detection"""
    try:
        logger.info("Starting livestream search...")
        
        # First get the uploads playlist ID
        channels_response = youtube.channels().list(
            id=CHANNEL_ID,
            part="contentDetails"
        ).execute()

        if not channels_response.get("items"):
            logger.error("Channel not found or no content details available")
            return None

        uploads_playlist_id = channels_response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        logger.info(f"Found uploads playlist ID: {uploads_playlist_id}")

        # Get recent videos from the uploads playlist
        playlist_items = youtube.playlistItems().list(
            playlistId=uploads_playlist_id,
            part="snippet",
            maxResults=500  # Start with 50 for testing, can increase later
        ).execute()

        if not playlist_items.get("items"):
            logger.error("No videos found in uploads playlist")
            return None

        logger.info(f"Checking {len(playlist_items['items'])} most recent videos...")
        
        # Sort items by published date (newest first) since API doesn't guarantee order
        sorted_items = sorted(
            playlist_items["items"],
            key=lambda x: x["snippet"]["publishedAt"],
            reverse=True
        )
        
        for index, item in enumerate(sorted_items):
            video_id = item["snippet"]["resourceId"]["videoId"]
            video_title = item["snippet"]["title"]
            published_at = item["snippet"]["publishedAt"]
            
            logger.info(f"\nChecking video #{index+1}: {video_title}")
            logger.info(f"Published: {published_at} | ID: {video_id}")

            # Get extended video details
            video_response = youtube.videos().list(
                id=video_id,
                part="liveStreamingDetails,snippet,status,contentDetails",
                fields="items(id,snippet(title,publishedAt,liveBroadcastContent),"
                      "liveStreamingDetails(actualStartTime,actualEndTime),"
                      "status(privacyStatus),contentDetails(duration))"
            ).execute()

            if not video_response.get("items"):
                logger.info("No video details found - skipping")
                continue

            video = video_response["items"][0]
            
            # Skip private/deleted videos
            if video["status"]["privacyStatus"] != "public":
                logger.info(f"Skipping {video['status']['privacyStatus']} video")
                continue

            # Check livestream markers (multiple ways)
            is_livestream = False
            if "liveStreamingDetails" in video:
                is_livestream = True
                logger.info("Has liveStreamingDetails")
            elif video["snippet"].get("liveBroadcastContent") == "live":
                is_livestream = True
                logger.info("Marked as live in snippet")
            elif "duration" in video["contentDetails"] and video["contentDetails"]["duration"] == "P0D":
                is_livestream = True
                logger.info("Has 0 duration (livestream indicator)")

            if not is_livestream:
                logger.info("Not a livestream - skipping")
                continue

            # Check for completion
            if "liveStreamingDetails" in video and "actualEndTime" in video["liveStreamingDetails"]:
                end_time = video["liveStreamingDetails"]["actualEndTime"]
                logger.info(f"Found COMPLETED livestream! Ended at: {end_time}")
                return {
                    "id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": video["snippet"]["title"],
                    "publishedAt": video["snippet"]["publishedAt"],
                    "endTime": end_time
                }
            else:
                logger.info("Livestream found but not yet ended")

        logger.info("No completed livestreams found in recent videos")
        return None

    except HttpError as e:
        logger.error(f"YouTube API Error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in get_latest_completed_livestream: {e}")
        return None


def download_video(video_id, filename):
    """Download video using yt-dlp"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        'outtmpl': os.path.join(DOWNLOAD_DIR, filename),
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
        return None

    if title is None:
        title = os.path.splitext(os.path.basename(file_path))[0]

    if description is None:
        description = read_description()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["clouds timelapse","weather timelapse","relaxing sky","4K nature", "ASMR clouds"], #["livestream", "timelapse"],
            "categoryId": "22"  # People & Blogs
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False
        }
    }

    try:
        # Disable cache to avoid warning
        #import googleapiclient.discovery_cache
        #googleapiclient.discovery_cache.autodiscover = False

        #def progress_callback(progress):
        #    logger.info(f"Upload progress: {progress.progress()}%")

        media = MediaFileUpload(
            file_path,
            chunksize=-1,
            resumable=True #,
            #progress_callback=progress_callback
        )

        request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media
        )

        logger.info(f"Uploading {file_path}...")
        #response = None
        
        response = request.execute()
        logger.info(f"Upload successful! Video ID: {response.get('id')}")
        return response
        #finally:
            # Ensure media object is properly closed
            #if hasattr(media, '_fd') and media._fd:
            #    media._fd.close()
        #except Exception as e:
        #    print(f"An error occurred during upload: {e}")
        #    return False

    except HttpError as e:
        logger.error(f"YouTube API error during upload: {e}")
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return False
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


def process_video():
    """Main processing function to find, download, process and upload livestreams"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Checking for completed livestreams...")

    try:
        youtube = setup_youtube_client()
        if youtube is None:
            logger.error("Failed to initialize YouTube client")
            return

        livestream = get_latest_completed_livestream(youtube)
        if not livestream:
            logger.info("No completed livestreams found in recent videos")
            return

        processed_urls = load_processed_urls()
        
        if livestream["url"] in processed_urls:
            logger.info(f"Livestream {livestream['url']} already processed. Skipping.")
            return

        logger.info(f"Found new livestream: {livestream['title']}")
        logger.info(f"Stream ended at: {livestream['endTime']}")

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        input_filename = "input.mp4"
        input_path = os.path.join(DOWNLOAD_DIR, input_filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"livestream_{timestamp}.mp4"
        output_path = os.path.join(DOWNLOAD_DIR, output_filename)

        logger.info("Downloading video...")
        if not download_video(livestream["id"], input_filename):
            logger.error("Failed to download video")
            return

        # Only save URL after successful download
        if os.path.exists(input_path) and os.path.getsize(input_path) > 0:
            save_processed_url(livestream["url"])
            logger.info(f"Saved URL to {URLS_FILE} after successful download")
        else:
            logger.error("Downloaded file is missing or empty")
            return

        logger.info("Processing with FFmpeg...")
        os.system(FFMPEG_CMD.format(input_file=input_path,random_start=random.randint(0, 1560), output_file=output_path))

        try:
            os.remove(input_path)
            logger.info(f"Processing complete. Output saved as {output_filename}")
        except OSError as e:
            logger.error(f"Error removing temporary file: {e}")

        # Upload the processed video
        logger.info("Preparing to upload video...")
        youtube_upload = get_authenticated_service()
        #video_title = f"Timelapse: {livestream['title']}"
        #description = f"Timelapse created from livestream on {livestream['endTime']}\n\nOriginal stream: {livestream['url']}"
        video_title = generate_video_title(livestream["title"], livestream["endTime"])
        description = generate_description(livestream)
        response = upload_video(youtube_upload, output_path, video_title, description)
        if response:
            logger.info("Video uploaded successfully!")
            # Get the uploaded video's URL
            uploaded_video_url = f"https://www.youtube.com/watch?v={response.get('id')}"
            #description = generate_description(livestream)
            # Update the original video's description
            if update_video_description(youtube_upload, livestream["id"], uploaded_video_url):
            	logger.info(f"Successfully updated original video description with timelapse link: {uploaded_video_url}")
            try:
            	os.remove(output_path)
            	logger.info(f"Deleted local timelapse file: {output_filename}")
            except OSError as e:
                logger.error(f"Error deleting timelapse file: {e}")
        else:
            logger.error("Video upload failed. Keeping local timelapse file.")

    except Exception as e:
        logger.error(f"Unexpected error in process_video: {e}")

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
    # Select a random template for the detected content type
    template = random.choice(templates[content_type])
    
    # Format the date
    end_date = datetime.strptime(end_time, "%Y-%m-%dT%H:%M:%SZ")
    formatted_date = end_date.strftime("%B %d, %Y")
    
    # Fill in the template
    speed = 60  # Your timelapse speed
    duration = "for studying"  # You could calculate this from the livestream duration
    
    return template.format(
        speed=speed,
        date=formatted_date,
        duration=duration
    )
def main():
    parser = argparse.ArgumentParser(description='YouTube Livestream Processor')
    parser.add_argument('--run-once', action='store_true', help='Run once and exit instead of scheduling')
    args = parser.parse_args()

    logger.info("YouTube Livestream Processor started")
    logger.info(f"Will check every {CHECK_INTERVAL} minutes")
    logger.info(f"Download directory: {os.path.abspath(DOWNLOAD_DIR)}")
    logger.info(f"Processed URLs stored in: {os.path.abspath(URLS_FILE)}")

    if args.run_once:
        process_video()
    else:
        # Initial run
        process_video()

        # Schedule periodic runs
        schedule.every(CHECK_INTERVAL).minutes.do(process_video)

        while True:
            schedule.run_pending()
            time.sleep(60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Script stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
