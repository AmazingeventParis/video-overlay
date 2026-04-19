"""
Service d'incrustation de masque Cinema sur les videos MyShootnbox / MySmakk.
Telecharge la video depuis OVH S3, applique l'overlay ffmpeg, re-upload sur S3.
"""
import os
import subprocess
import tempfile
import requests
import boto3
from flask import Flask, request, jsonify

app = Flask(__name__)

# OVH S3 config
S3_ACCESS_KEY = "3d0274222c2a41fa8bb7dbd0248e8527"
S3_SECRET_KEY = "ddb9ce6f823045ef81952928666a7646"
S3_ENDPOINT = "https://s3.sbg.io.cloud.ovh.net"
S3_BUCKET = "app-media-shootnbox"
S3_PUBLIC_HOST = "app-media-shootnbox.s3.sbg.io.cloud.ovh.net"

s3_client = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    region_name="sbg",
)


def build_color_filter(matrix):
    """Construit le filtre ffmpeg geq depuis une matrice 5x4.
    Utilise geq pour appliquer la formule complete sans clamping intermediaire,
    ce qui donne un rendu identique a Flutter ColorFilter.matrix."""
    if not matrix or len(matrix) != 20:
        return None

    # geq applique la formule pixel par pixel: output = clamp(m0*r + m1*g + m2*b + m3*a + m4)
    r_expr = f"clip({matrix[0]:.6f}*r(X,Y) + {matrix[1]:.6f}*g(X,Y) + {matrix[2]:.6f}*b(X,Y) + {matrix[3]:.6f}*alpha(X,Y) + {matrix[4]:.2f}, 0, 255)"
    g_expr = f"clip({matrix[5]:.6f}*r(X,Y) + {matrix[6]:.6f}*g(X,Y) + {matrix[7]:.6f}*b(X,Y) + {matrix[8]:.6f}*alpha(X,Y) + {matrix[9]:.2f}, 0, 255)"
    b_expr = f"clip({matrix[10]:.6f}*r(X,Y) + {matrix[11]:.6f}*g(X,Y) + {matrix[12]:.6f}*b(X,Y) + {matrix[13]:.6f}*alpha(X,Y) + {matrix[14]:.2f}, 0, 255)"

    return f"geq=r='{r_expr}':g='{g_expr}':b='{b_expr}'"


def build_ffmpeg_filter(event_name: str, event_type: str, duration: float, brand: str = "SHOOTNBOX"):
    """Construit le filtre ffmpeg pour l'overlay Cinema."""

    full_event = event_name if event_name else ""

    # Echapper les caractères spéciaux pour ffmpeg drawtext
    full_event = full_event.replace("'", "\\'").replace(":", "\\:")

    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    font_light = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

    filters = [
        # REC dot (rouge, clignotant)
        "drawbox=x=24:y=20:w=20:h=20:color=red:t=fill:enable='lt(mod(t\\,1)\\,0.5)'",
        # REC text (blanc, clignotant)
        f"drawtext=text='REC':x=50:y=16:fontsize=28:fontfile='{font}':fontcolor=white:shadowcolor=black@0.5:shadowx=1:shadowy=1:enable='lt(mod(t\\,1)\\,0.5)'",
        # Timer top right
        f"drawtext=text='%{{pts\\:gmtime\\:0\\:%M\\\\\\:%S}}':x=w-170:y=16:fontsize=36:fontfile='{font_light}':fontcolor=white:shadowcolor=black@0.5:shadowx=1:shadowy=1",

        # Event name (rose, gras, net)
        f"drawtext=text='{full_event}':x=(w-text_w)/2:y=62:fontsize=30:fontfile='{font}':fontcolor=0xFF1493:shadowcolor=black@0.4:shadowx=1:shadowy=1",

        # Corner brackets - top left
        f"drawbox=x=25:y=100:w=70:h=4:color=white@0.9:t=fill",
        f"drawbox=x=25:y=100:w=4:h=70:color=white@0.9:t=fill",
        # Corner brackets - top right
        f"drawbox=x='iw-95':y=100:w=70:h=4:color=white@0.9:t=fill",
        f"drawbox=x='iw-29':y=100:w=4:h=70:color=white@0.9:t=fill",
        # Corner brackets - bottom left
        f"drawbox=x=25:y='ih-130':w=70:h=4:color=white@0.9:t=fill",
        f"drawbox=x=25:y='ih-200':w=4:h=70:color=white@0.9:t=fill",
        # Corner brackets - bottom right
        f"drawbox=x='iw-95':y='ih-130':w=70:h=4:color=white@0.9:t=fill",
        f"drawbox=x='iw-29':y='ih-200':w=4:h=70:color=white@0.9:t=fill",

        # Progress bar background
        f"drawbox=x=20:y='ih-100':w='iw-40':h=4:color=white@0.2:t=fill",
        # Progress bar fill (rose, avance avec le temps)
        f"drawbox=x=20:y='ih-100':w='(iw-40)*t/{duration}':h=4:color=0xFF1493:t=fill",

        # Brand label (centré, plus grand)
        f"drawtext=text='{brand}':x=(w-text_w)/2:y=h-85:fontsize=22:fontfile='{font}':fontcolor=white@0.7:shadowcolor=black@0.4:shadowx=1:shadowy=1",
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


def upload_to_s3(filepath: str, s3_key: str, content_type: str = "video/mp4") -> str:
    """Upload un fichier vers OVH S3 et retourne l'URL publique."""
    s3_client.upload_file(
        filepath, S3_BUCKET, s3_key,
        ExtraArgs={"ContentType": content_type, "ACL": "public-read"}
    )
    return f"https://{S3_PUBLIC_HOST}/{s3_key}"


@app.route("/process", methods=["POST"])
def process_video():
    """
    POST /process
    Body JSON: { "video_url": "...", "storage_path": "...", "s3_key": "...",
                 "event_name": "...", "event_type": "...", "brand": "..." }
    """
    data = request.json
    video_url = data.get("video_url")
    storage_path = data.get("storage_path")
    s3_key = data.get("s3_key", "")
    event_name = data.get("event_name", "")
    event_type = data.get("event_type", "soiree")
    brand = data.get("brand", "SHOOTNBOX")
    color_matrix = data.get("color_matrix")

    if not video_url:
        return jsonify({"error": "video_url requis"}), 400

    # Determiner la cle S3 pour le re-upload
    if not s3_key and storage_path:
        # Extraire la cle S3 depuis l'URL si possible
        if S3_PUBLIC_HOST in video_url:
            s3_key = video_url.split(f"https://{S3_PUBLIC_HOST}/")[1]
        else:
            s3_key = storage_path

    if not s3_key:
        return jsonify({"error": "s3_key ou storage_path requis"}), 400

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
            vf = build_ffmpeg_filter(event_name, event_type, duration, brand)

            # Ajouter le filtre couleur si present
            color_vf = build_color_filter(color_matrix)
            full_vf = f"{color_vf},{vf}" if color_vf else vf
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-vf", full_vf,
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-preset", "fast",
                "-crf", "14",
                output_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                return jsonify({"error": "ffmpeg failed", "stderr": result.stderr[-1000:]}), 500

            # Re-upload vers OVH S3 (remplace l'original)
            new_url = upload_to_s3(output_path, s3_key)

            return jsonify({"success": True, "url": new_url})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/thumbnail", methods=["POST"])
def generate_thumbnail():
    """
    POST /thumbnail
    Body JSON: { "video_url": "...", "storage_path": "...", "s3_key": "..." }
    Genere un thumbnail depuis la video et l'upload sur S3.
    """
    data = request.json
    video_url = data.get("video_url")
    storage_path = data.get("storage_path", "")
    s3_key = data.get("s3_key", "")
    color_matrix = data.get("color_matrix")

    if not video_url:
        return jsonify({"error": "video_url requis"}), 400

    # Determiner la cle S3 pour le thumbnail
    if not s3_key:
        if S3_PUBLIC_HOST in video_url:
            s3_key = video_url.split(f"https://{S3_PUBLIC_HOST}/")[1]
        elif storage_path:
            s3_key = storage_path

    # Remplacer .mp4 par _thumb.jpg
    thumb_key = s3_key.rsplit('.', 1)[0] + '_thumb.jpg' if s3_key else ""

    if not thumb_key:
        return jsonify({"error": "impossible de determiner le chemin du thumbnail"}), 400

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "input.mp4")
            thumb_path = os.path.join(tmpdir, "thumb.jpg")

            # Telecharger la video
            resp = requests.get(video_url, timeout=120)
            resp.raise_for_status()
            with open(input_path, "wb") as f:
                f.write(resp.content)

            # Extraire le thumbnail a 1 seconde (avec filtre couleur si present)
            color_vf = build_color_filter(color_matrix)
            vf_parts = []
            if color_vf:
                vf_parts.append(color_vf)
            vf_parts.append("scale=480:-1")
            thumb_vf = ",".join(vf_parts)

            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-ss", "1", "-vframes", "1",
                "-vf", thumb_vf,
                thumb_path
            ]
            subprocess.run(cmd, capture_output=True, timeout=60)

            if not os.path.exists(thumb_path):
                return jsonify({"error": "thumbnail generation failed"}), 500

            # Upload vers S3
            thumb_url = upload_to_s3(thumb_path, thumb_key, "image/jpeg")

            return jsonify({"success": True, "thumbnail_url": thumb_url})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050)
