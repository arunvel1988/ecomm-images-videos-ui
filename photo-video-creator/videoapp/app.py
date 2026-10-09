import os
import uuid
import shutil
import subprocess
from pathlib import Path
from flask import Flask, request, render_template, send_from_directory, flash, redirect, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
DEFAULT_AUDIO = BASE_DIR / "default_music.mp3"

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

ALLOWED_IMAGES = {"jpg", "jpeg", "png", "webp"}
ALLOWED_AUDIO = {"mp3", "wav", "m4a", "aac", "ogg"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024


def run(cmd):
    print("RUN:", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def ensure_default_audio():
    """Create a royalty-free synthetic ambient track if one doesn't exist."""
    if DEFAULT_AUDIO.exists():
        return

    # Simple synthetic chord bed. No external music file is required.
    filter_complex = (
        "[0:a]volume=0.10[a0];"
        "[1:a]volume=0.055[a1];"
        "[2:a]volume=0.04[a2];"
        "[a0][a1][a2]amix=inputs=3:duration=longest:normalize=0,"
        "lowpass=f=1800,afade=t=in:st=0:d=2,afade=t=out:st=55:d=5[a]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=60",
        "-f", "lavfi", "-i", "sine=frequency=277.18:duration=60",
        "-f", "lavfi", "-i", "sine=frequency=329.63:duration=60",
        "-filter_complex", filter_complex,
        "-map", "[a]",
        "-c:a", "libmp3lame", "-b:a", "128k",
        str(DEFAULT_AUDIO),
    ]
    run(cmd)


def make_image_segment(image_path: Path, segment_path: Path, duration: float, index: int):
    """Create a vertical cinematic segment while preserving the ENTIRE source photo.

    The old implementation used scale(...increase)+crop(...), which is a center crop.
    That is bad for product photos containing multiple items (for example three jerseys).

    This implementation creates:
      1. A blurred, full-screen version of the image as the background.
      2. A complete, aspect-ratio-preserving foreground image.
      3. A very small camera drift so the whole photo remains visible while the video
         still feels animated.
    """
    frames = int(duration * 30)

    # Keep the complete source image inside a 1040x1840 safe area.
    # It is then placed over a blurred 1080x1920 background.
    # The foreground movement is intentionally tiny so no part of the source image
    # disappears from the frame.
    if index % 4 == 0:
        x_expr = "20+8*sin(2*PI*n/{})".format(frames)
        y_expr = "40+5*sin(2*PI*n/{})".format(frames)
    elif index % 4 == 1:
        x_expr = "12+10*sin(PI*n/{})".format(frames)
        y_expr = "40+6*cos(PI*n/{})".format(frames)
    elif index % 4 == 2:
        x_expr = "20+8*cos(2*PI*n/{})".format(frames)
        y_expr = "35+5*sin(2*PI*n/{})".format(frames)
    else:
        x_expr = "15+10*sin(PI*n/{})".format(frames)
        y_expr = "38+6*cos(PI*n/{})".format(frames)

    vf = (
        "split=2[bg][fg];"
        # Full-screen blurred background. This avoids black bars while never
        # cropping the foreground product photo.
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase," 
        "crop=1080:1920,boxblur=luma_radius=28:luma_power=2," 
        "eq=brightness=-0.03:saturation=0.80[bg2];"
        # Foreground: FIT, don't crop. Entire source image remains visible.
        "[fg]scale=1040:1840:force_original_aspect_ratio=decrease," 
        "format=rgba,pad=1040:1840:(ow-iw)/2:(oh-ih)/2:color=black@0," 
        "eq=contrast=1.04:saturation=1.08:brightness=0.01," 
        "unsharp=5:5:0.35:5:5:0[fg2];"
        # Put the complete photo on top and give it a very gentle camera drift.
        f"[bg2][fg2]overlay=x='{x_expr}':y='{y_expr}':eval=frame," 
        "format=yuv420p"
    )

    run([
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(image_path),
        "-t", str(duration),
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "21",
        "-pix_fmt", "yuv420p",
        str(segment_path),
    ])


def concat_segments(segment_paths, concat_file: Path, output_path: Path):
    with concat_file.open("w", encoding="utf-8") as f:
        for path in segment_paths:
            f.write(f"file '{path.as_posix().replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n")

    run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(output_path),
    ])


def add_audio(video_path: Path, audio_path: Path, final_path: Path):
    # The music is looped if it is shorter than the video and trimmed if longer.
    run([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-stream_loop", "-1",
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "160k",
        "-shortest",
        "-movflags", "+faststart",
        str(final_path),
    ])


def create_video(image_files, music_file=None, seconds_per_image=4):
    job_id = uuid.uuid4().hex
    work_dir = OUTPUT_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        image_paths = []
        for i, uploaded in enumerate(image_files):
            filename = secure_filename(uploaded.filename or f"image_{i}.jpg")
            if not filename or Path(filename).suffix.lower().lstrip(".") not in ALLOWED_IMAGES:
                continue
            path = work_dir / f"image_{i:03d}{Path(filename).suffix.lower()}"
            uploaded.save(path)
            image_paths.append(path)

        if not image_paths:
            raise ValueError("No valid images were uploaded.")

        audio_path = DEFAULT_AUDIO
        if music_file and music_file.filename:
            music_name = secure_filename(music_file.filename)
            ext = Path(music_name).suffix.lower().lstrip(".")
            if ext in ALLOWED_AUDIO:
                audio_path = work_dir / f"music.{ext}"
                music_file.save(audio_path)

        segments = []
        for i, image_path in enumerate(image_paths):
            segment = work_dir / f"segment_{i:03d}.mp4"
            make_image_segment(image_path, segment, seconds_per_image, i)
            segments.append(segment)

        silent_video = work_dir / "silent.mp4"
        concat_file = work_dir / "concat.txt"
        concat_segments(segments, concat_file, silent_video)

        final_video = OUTPUT_DIR / f"slideshow_{job_id}.mp4"
        add_audio(silent_video, audio_path, final_video)

        shutil.rmtree(work_dir, ignore_errors=True)
        return final_video.name

    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/create", methods=["POST"])
def create():
    images = request.files.getlist("images")
    music = request.files.get("music")

    try:
        seconds = float(request.form.get("seconds_per_image", "4"))
        seconds = min(max(seconds, 2.0), 10.0)
    except ValueError:
        seconds = 4.0

    if not images or all(not x.filename for x in images):
        flash("Please upload at least one image.")
        return redirect(url_for("index"))

    try:
        ensure_default_audio()
        filename = create_video(images, music, seconds)
        return render_template("result.html", filename=filename)
    except subprocess.CalledProcessError as exc:
        error_text = exc.stderr.decode("utf-8", errors="ignore")[-2000:]
        app.logger.exception("FFmpeg failed")
        return f"Video generation failed:<pre>{error_text}</pre>", 500
    except Exception as exc:
        app.logger.exception("Video generation failed")
        return f"Video generation failed: {exc}", 500


@app.route("/video/<path:filename>")
def video(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


@app.route("/download/<path:filename>")
def download(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    ensure_default_audio()
    app.run(host="0.0.0.0", port=5000, debug=False)
