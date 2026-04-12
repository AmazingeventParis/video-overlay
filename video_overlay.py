"""
Service d'incrustation de masque Cinema sur les videos MyShootnbox.
Telecharge la video depuis Supabase, applique l'overlay ffmpeg, re-upload.
"""
import os
import subprocess
import tempfile
import requests
from flask import Flask, request, jsonify
from supabase import create_client

app = Flask(__name__)

SUPABASE_URL = "https://supabase-api.swipego.app"
SUPABASE_KEY = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJzdXBhYmFzZSIsImlhdCI6MTc3MTI3NDIyMCwiZXhwIjo0OTI2OTQ3ODIwLCJyb2xlIjoic2VydmljZV9yb2xlIn0.iqPsHjDWX9X2942nD1lsSin0yNvob06s0qP_FDTShns"
BUCKET = "appshoot-photos"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def build_ffmpeg_filter(event_name: str, event_type: str, duration: float):
    """Construit le filtre ffmpeg pour l'overlay Cinema."""

    prefix_map = {
        'mariage': 'Mariage de',
        'anniversaire': 'Anniversaire de',
        'entreprise': 'Evenement de',
        'soiree': 'Soiree de',
    }
    prefix = prefix_map.get(event_type, 'Evenement')
    full_event = f"{prefix} {event_name}"

    # Echapper les caractères spéciaux pour ffmpeg drawtext
    full_event = full_event.replace("'", "\\'").replace(":", "\\:")

    filters = [
        # REC dot (rouge, clignotant) - gros
        "drawbox=x=30:y=40:w=24:h=24:color=red:t=fill:enable='lt(mod(t,1),0.5)'",
        # REC text (blanc, clignotant) - gros
        "drawtext=text=REC:x=65:y=38:fontsize=30:fontcolor=white:enable='lt(mod(t,1),0.5)'",
        # Timer top right - gros
        "drawtext=text='%{pts\\:gmtime\\:0\\:%M\\:%S}':x=w-160:y=38:fontsize=36:fontcolor=white",

        # Corner brackets - epais
        "drawbox=x=50:y=120:w=60:h=4:color=white@0.6:t=fill",
        "drawbox=x=50:y=120:w=4:h=60:color=white@0.6:t=fill",
        "drawbox=x=iw-110:y=120:w=60:h=4:color=white@0.6:t=fill",
        "drawbox=x=iw-54:y=120:w=4:h=60:color=white@0.6:t=fill",
        "drawbox=x=50:y=ih-240:w=60:h=4:color=white@0.6:t=fill",
        "drawbox=x=50:y=ih-300:w=4:h=60:color=white@0.6:t=fill",
        "drawbox=x=iw-110:y=ih-240:w=60:h=4:color=white@0.6:t=fill",
        "drawbox=x=iw-54:y=ih-300:w=4:h=60:color=white@0.6:t=fill",

        # Progress bar background - epais
        "drawbox=x=30:y=ih-180:w=iw-60:h=6:color=white@0.2:t=fill",

        # SHOOTNBOX label - gros
        "drawtext=text=SHOOTNBOX:x=30:y=h-155:fontsize=22:fontcolor=white@0.6",

        # Timer label en bas a droite
        "drawtext=text='%{pts\\:gmtime\\:0\\:%M\\:%S} / 00\\:30':x=w-250:y=h-155:fontsize=22:fontcolor=white@0.6",

        # Event name (rose) - gros
        f"drawtext=text='{full_event}':x=(w-text_w)/2:y=h-120:fontsize=28:fontcolor=pink",
    ]

    return ",".join(filters)


def get_video_duration(filepath: str) -> float:
    """Recupere la duree de la video en secondes."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", filepath],
        capture_output=True, text=True
    )
    try:
        return float(result.stdout.strip())
    except:
        return 30.0


@app.route("/process", methods=["POST"])
def process_video():
    """
    POST /process
    Body JSON: { "video_url": "...", "storage_path": "...", "event_name": "...", "event_type": "..." }
    """
    data = request.json
    video_url = data.get("video_url")
    storage_path = data.get("storage_path")
    event_name = data.get("event_name", "")
    event_type = data.get("event_type", "soiree")

    if not video_url or not storage_path:
        return jsonify({"error": "video_url et storage_path requis"}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "input.mp4")
            output_path = os.path.join(tmpdir, "output.mp4")

            # Telecharger la video
            resp = requests.get(video_url, timeout=120)
            resp.raise_for_status()
            with open(input_path, "wb") as f:
                f.write(resp.content)

            # Duree
            duration = get_video_duration(input_path)

            # Construire le filtre
            vf = build_ffmpeg_filter(event_name, event_type, duration)

            # Appliquer ffmpeg
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-vf", vf,
                "-c:a", "copy",
                "-preset", "fast",
                "-crf", "23",
                output_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                return jsonify({"error": "ffmpeg failed", "stderr": result.stderr[-1500:], "cmd": " ".join(cmd)}), 500

            # Re-upload vers Supabase Storage (remplace l'original)
            with open(output_path, "rb") as f:
                file_data = f.read()

            supabase.storage.from_(BUCKET).update(
                storage_path,
                file_data,
                file_options={"content-type": "video/mp4", "upsert": "true"}
            )

            # URL publique (identique car meme path)
            new_url = supabase.storage.from_(BUCKET).get_public_url(storage_path)

            return jsonify({"success": True, "url": new_url})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)
