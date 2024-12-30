import os
import io
import base64
import sqlite3
from datetime import datetime, timedelta
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from flask import Flask, render_template, redirect, url_for, session, request
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", 'your_key')  # Change this to a random secure key

# Spotify API credentials
SPOTIPY_CLIENT_ID = 'your_key'
SPOTIPY_CLIENT_SECRET = 'your_key'
SPOTIPY_REDIRECT_URI = 'http://localhost:5000/callback/'  # can be changed if we deploy to a different domain

# Scope for accessing user data
SCOPE = "user-top-read user-library-read user-read-playback-state"

# Create a connection to the SQLite database
db_path = os.path.join(os.path.dirname(__file__), 'data.db')
conn = sqlite3.connect(db_path)
conn.execute('''CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spotify_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    access_token TEXT NOT NULL,
    token_type TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    refresh_token TEXT NOT NULL
)''')
conn.commit()
conn.close()


def authenticate_spotify():
    sp_oauth = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE
    )

    # Validate the state to prevent cross-site request forgery (CSRF) attacks
    if request.args.get('state') != session.get('oauth_state'):
        return 'Invalid state parameter', 400

    # Check if the user is already authenticated and has an access token
    if 'token_info' in session:
        token_info = session['token_info']
        expires_at = datetime.fromtimestamp(token_info.get('expires_at'))
        if expires_at > datetime.now():
            # If the access token is not expired, use it directly
            return redirect(url_for('dashboard'))

        # If the access token is expired, try to refresh it
        sp_oauth._refresh_access_token(token_info.get('refresh_token'))
        token_info = sp_oauth.get_cached_token()
        if not token_info:
            return 'Error: Unable to refresh token', 400

    else:
        # If the user is not authenticated, redirect to Spotify login
        auth_url = sp_oauth.get_authorize_url()
        session['oauth_state'] = sp_oauth.state
        session['oauth_scope'] = SCOPE
        return redirect(auth_url)

    access_token = token_info.get('access_token')
    refresh_token = token_info.get('refresh_token')
    expires_at = datetime.now() + timedelta(seconds=token_info.get('expires_in', 0))

    sp = spotipy.Spotify(auth=access_token)
    user_info = sp.current_user()

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if the user already exists in the database
    cursor.execute("SELECT id FROM users WHERE spotify_id=?", (user_info['id'],))
    user_id = cursor.fetchone()

    if not user_id:
        # If the user does not exist, insert the new user data into the database
        cursor.execute("INSERT INTO users (spotify_id, display_name, access_token, token_type, expires_at, "
                       "refresh_token) VALUES (?, ?, ?, ?, ?, ?)",
                       (user_info['id'], user_info['display_name'], access_token, token_info.get('token_type'),
                        expires_at.timestamp(), refresh_token))
        conn.commit()

    cursor.close()
    conn.close()

    session['username'] = user_info['display_name']
    session['token_info'] = token_info  # Store the token_info in the session

    return redirect(url_for('dashboard'))


def get_audio_features(sp, track_ids):
    audio_features = []
    for i in range(0, len(track_ids), 50):  # Spotify API supports fetching up to 50 tracks at once
        batch_ids = track_ids[i:i + 50]
        batch_features = sp.audio_features(batch_ids)
        audio_features.extend(batch_features)
    return audio_features


def cosine_similarity(v1, v2):
    dot_product = sum(x * y for x, y in zip(v1, v2))
    norm_v1 = sum(x * x for x in v1) ** 0.5
    norm_v2 = sum(y * y for y in v2) ** 0.5
    return dot_product / (norm_v1 * norm_v2)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/login')
def login():
    if session.get('token_info'):
        # If the user is already authenticated, redirect to the dashboard
        return redirect(url_for('dashboard'))

    sp_oauth = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE
    )
    auth_url = sp_oauth.get_authorize_url()

    # Store the state and scope in the session for later validation in the callback
    session['oauth_state'] = sp_oauth.state
    session['oauth_scope'] = SCOPE

    return redirect(auth_url)


def get_user_top_genres(sp):
    top_tracks = sp.current_user_top_tracks(limit=50, time_range='long_term')
    genre_counter = {}
    total_tracks = len(top_tracks['items'])

    for track in top_tracks['items']:
        track_info = sp.track(track['id'])
        if 'genres' in track_info:
            for genre in track_info['genres']:
                genre_counter[genre] = genre_counter.get(genre, 0) + 1

    top_genres = {}
    for genre, count in genre_counter.items():
        percentage = (count / total_tracks) * 100
        top_genres[genre] = percentage

    return top_genres


@app.route('/callback/')
def callback():
    if session.get('token_info'):
        # If the user is already authenticated, redirect to the dashboard
        return redirect(url_for('dashboard'))

    sp_oauth = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE
    )

    # Validate the state to prevent cross-site request forgery (CSRF) attacks
    if request.args.get('state') != session.get('oauth_state'):
        return 'Invalid state parameter', 400

    # Use get_cached_token instead of get_access_token
    token_info = sp_oauth.get_cached_token()

    if not token_info:
        return 'Error: Unable to fetch token', 400

    access_token = token_info.get('access_token')
    refresh_token = token_info.get('refresh_token')
    expires_at = datetime.now() + timedelta(seconds=token_info.get('expires_in', 0))

    # Check if the access token is still valid
    if expires_at <= datetime.now():
        # If the access token is expired, try to refresh it
        token_info = sp_oauth.refresh_access_token(refresh_token)
        access_token = token_info.get('access_token')
        expires_at = datetime.now() + timedelta(seconds=token_info.get('expires_in', 0))

    sp = spotipy.Spotify(auth=access_token)
    user_info = sp.current_user()

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if the user already exists in the database
    cursor.execute("SELECT id FROM users WHERE spotify_id=?", (user_info['id'],))
    user_id = cursor.fetchone()

    if not user_id:
        # If the user does not exist, insert the new user data into the database
        cursor.execute("INSERT INTO users (spotify_id, display_name, access_token, token_type, expires_at, "
                       "refresh_token) VALUES (?, ?, ?, ?, ?, ?)",
                       (user_info['id'], user_info['display_name'], access_token, token_info.get('token_type'),
                        expires_at.timestamp(), refresh_token))
        conn.commit()

    cursor.close()
    conn.close()

    # Add the username to the session
    session['username'] = user_info['display_name']
    session['token_info'] = token_info  # Store the token_info in the session

    return redirect(url_for('dashboard'))


'''@app.route('/dashboard')
def dashboard():
    if not session.get('token_info'):
        return redirect(url_for('login'))

    sp = spotipy.Spotify(auth=session['token_info']['access_token'])


   # Initialize user_data and chart_image with None
    user_data = {}
    chart_image = None
    
    try:
        top_tracks_week = sp.current_user_top_tracks(limit=5, time_range='short_term')
        top_tracks_month = sp.current_user_top_tracks(limit=5, time_range='medium_term')
        #top_tracks_all_time = sp.current_user_top_tracks(limit=5, time_range='long_term')

        # Get recommended songs
        recommended_tracks = sp.recommendations(seed_tracks=[track['id'] for track in top_tracks_week['items']], limit=5)
        user_data['recommended_songs'] = recommended_tracks['tracks']

        # Get recommended playlists
        recommended_playlists = sp.recommendations(seed_genres=list(user_data['top_genres'].keys()), limit=5)
        user_data['recommended_playlists'] = recommended_playlists['tracks']

        top_genres = get_user_top_genres(sp)

        top_artists_week = sp.current_user_top_artists(limit=5, time_range='short_term')
        top_artists_month = sp.current_user_top_artists(limit=5, time_range='medium_term')
        top_artists_all_time = sp.current_user_top_artists(limit=5, time_range='long_term')

        top_songs_week = sp.current_user_top_tracks(limit=5, time_range='short_term')
        top_songs_all_time = sp.current_user_top_tracks(limit=5, time_range='long_term')

        playlists = sp.current_user_playlists(limit=5)

        #current_playing_track = sp.current_playback()
        #total_time = current_playing_track.get('progress_ms', 0) // 1000 if current_playing_track else 0
        user_info = sp.current_user()
        user_data = {
            'profile_image': 'https://via.placeholder.com/150',
            'spotify_username': session['username'],
            'main_id': 'sample_main_id',
            'join_date': '2023-07-22',
            'top_tracks_week': top_tracks_week['items'],
            'top_tracks_month': top_tracks_month['items'],
            'top_artists_week': top_artists_week['items'],
            'top_artists_month': top_artists_month['items'],
            'top_artists_all_time': top_artists_all_time['items'],
            'top_songs_week': top_songs_week['items'],
            'top_songs_all_time': top_songs_all_time['items'],
            'top_playlists': [{'name': playlist['name'], 'owner': playlist['owner']['display_name'],
                               'image_url': 'https://via.placeholder.com/150'} for playlist in playlists['items']],
            'top_genres': top_genres,
            'recommended_songs': recommended_tracks['tracks'],
            'recommended_playlists': recommended_playlists['playlists'],
        }

        # Generate chart_image and update user_data with it
        top_genre_names = list(top_genres.keys())
        top_genre_percentages = list(top_genres.values())

        plt.figure(figsize=(8, 6))
        plt.bar(top_genre_names, top_genre_percentages)
        plt.xlabel('Genres')
        plt.ylabel('Percentage')
        plt.title('Top Genres')
        plt.xticks(rotation=45)

        buffer = io.BytesIO()
        plt.savefig(buffer, format='png')
        buffer.seek(0)
        plt.close()

        chart_image = base64.b64encode(buffer.getvalue()).decode('utf-8')
        user_data['chart_image'] = chart_image

        # Get the user's profile image URL from Spotify user_info
        user_data['profile_image'] = user_info.get('images')[0].get('url', 'https://via.placeholder.com/150')

    except Exception as e:
        error_message = f"Error: {e}"
        return render_template('dashboard.html', error_message=error_message, user_data=None)

    return render_template('dashboard.html', user_data=user_data)'''

    
@app.route('/dashboard')
def dashboard():
    if not session.get('token_info'):
        return redirect(url_for('login'))

    sp = spotipy.Spotify(auth=session['token_info']['access_token'])

    # Initialize user_data and chart_image with None
    user_data = {}
    chart_image = None

    try:
        top_tracks_week = sp.current_user_top_tracks(limit=5, time_range='short_term')
        top_tracks_month = sp.current_user_top_tracks(limit=5, time_range='medium_term')
        # top_tracks_all_time = sp.current_user_top_tracks(limit=5, time_range='long_term')

        seed_tracks = [f"spotify:track:{track['id']}" for track in top_tracks_week['items']]
        print("seed_tracks:", seed_tracks)

        recommended_tracks = sp.recommendations(seed_tracks=seed_tracks, limit=5)
        print("recommended_tracks:", recommended_tracks)

        user_data['recommended_songs'] = recommended_tracks['tracks']

        # Get top genres
        top_genres = get_user_top_genres(sp)

        top_artists_week = sp.current_user_top_artists(limit=5, time_range='short_term')
        top_artists_month = sp.current_user_top_artists(limit=5, time_range='medium_term')
        top_artists_all_time = sp.current_user_top_artists(limit=5, time_range='long_term')

        top_songs_week = sp.current_user_top_tracks(limit=5, time_range='short_term')
        top_songs_all_time = sp.current_user_top_tracks(limit=5, time_range='long_term')

        playlists = sp.current_user_playlists(limit=5)

        # current_playing_track = sp.current_playback()
        # total_time = current_playing_track.get('progress_ms', 0) // 1000 if current_playing_track else 0
        user_info = sp.current_user()
        user_data = {
            'profile_image': 'https://via.placeholder.com/150',
            'spotify_username': session['username'],
            'main_id': 'sample_main_id',
            'join_date': '2023-07-22',
            'top_tracks_week': top_tracks_week['items'],
            'top_tracks_month': top_tracks_month['items'],
            'top_artists_week': top_artists_week['items'],
            'top_artists_month': top_artists_month['items'],
            'top_artists_all_time': top_artists_all_time['items'],
            'top_songs_week': top_songs_week['items'],
            'top_songs_all_time': top_songs_all_time['items'],
            'top_playlists': [{'name': playlist['name'], 'owner': playlist['owner']['display_name'],
                               'image_url': 'https://via.placeholder.com/150'} for playlist in playlists['items']],
            'top_genres': top_genres,
            'recommended_songs': recommended_tracks['tracks'],
            'recommended_playlists': [],
        }

        # Fetch recommended playlists using the top genres
        recommended_playlists = sp.recommendations(seed_genres=list(top_genres.keys()), limit=5)
        user_data['recommended_playlists'] = recommended_playlists['tracks']

        # Generate chart_image and update user_data with it
        top_genre_names = list(top_genres.keys())
        top_genre_percentages = list(top_genres.values())

        plt.figure(figsize=(8, 6))
        plt.bar(top_genre_names, top_genre_percentages)
        plt.xlabel('Genres')
        plt.ylabel('Percentage')
        plt.title('Top Genres')
        plt.xticks(rotation=45)

        buffer = io.BytesIO()
        plt.savefig(buffer, format='png')
        buffer.seek(0)
        plt.close()

        chart_image = base64.b64encode(buffer.getvalue()).decode('utf-8')
        user_data['chart_image'] = chart_image

        # Get the user's profile image URL from Spotify user_info
        user_data['profile_image'] = user_info.get('images')[0].get('url', 'https://via.placeholder.com/150')

    except Exception as e:
        error_message = f"Error: {e}"
        return render_template('dashboard.html', error_message=error_message, user_data=None, chart_image=chart_image)

    return render_template('dashboard.html', user_data=user_data, chart_image=chart_image)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(debug=True, threaded=True)
