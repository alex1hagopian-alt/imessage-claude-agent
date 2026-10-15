# Turns iMessage attachments (photos, voice messages, files) into something Claude Code can use.
"""Attachment handling for the bridge.

`lookup(rowid)` reads a message's attachments from chat.db. `prepare()` copies them
into `<project>/.imessage-inbox/`, converts HEIC photos to JPEG, transcribes audio
with whisper.cpp, and returns a block of text to append to the user's message.
"""
import os
import re
import shutil
import subprocess
import tempfile
import time

import config
import imessage_agent as im

INBOX_DIR = '.imessage-inbox'
MAX_AGE_HOURS = 24  # inbox files older than this are deleted
MAX_BYTES = 50 * 1024 * 1024        # bigger files are not copied
TRANSCRIBE_TIMEOUT = 300            # seconds
MAX_TRANSCRIPT_CHARS = 8000

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
CONVERT_EXTS = {'.heic', '.heif', '.tiff', '.tif', '.bmp'}   # turned into JPEG with sips
AUDIO_EXTS = {'.caf', '.m4a', '.mp3', '.wav', '.aac', '.amr', '.aiff', '.ogg', '.opus'}
VIDEO_EXTS = {'.mov', '.mp4', '.m4v'}


def _bin(name):
    return shutil.which(name) or os.path.join('/opt/homebrew/bin', name)


def lookup(rowid):
    """Attachments of one message: [{'path', 'name', 'mime', 'expected'}], oldest first."""
    db = im.connect_db()
    try:
        rows = db.execute('''
            SELECT a.filename, a.transfer_name, a.mime_type, a.total_bytes
            FROM attachment a
            JOIN message_attachment_join j ON a.ROWID = j.attachment_id
            WHERE j.message_id = ? ORDER BY a.ROWID''', (rowid,)).fetchall()
    finally:
        db.close()
    return [{'path': os.path.expanduser(f) if f else '', 'name': n or '',
             'mime': m or '', 'expected': b or 0} for f, n, m, b in rows]


def is_ready(att):
    """The file is on disk and fully downloaded."""
    try:
        size = os.path.getsize(att['path'])
    except OSError:
        return False
    return size > 0 and (not att['expected'] or size >= att['expected'])


def kind(att):
    ext = os.path.splitext(att['name'] or att['path'])[1].lower()
    mime = att['mime']
    if ext in IMAGE_EXTS or ext in CONVERT_EXTS or mime.startswith('image/'):
        return 'image'
    if ext in AUDIO_EXTS or mime.startswith('audio/'):
        return 'audio'
    if ext in VIDEO_EXTS or mime.startswith('video/'):
        return 'video'
    return 'file'


def _safe_name(name):
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', os.path.basename(name)).strip('._')
    return name[-60:] or 'file'


def transcribe(path):
    """Speech to text with whisper.cpp. Returns (text, None) or (None, why it failed)."""
    cli, model = _bin('whisper-cli'), config.WHISPER_MODEL
    if not os.path.exists(cli):
        return None, 'whisper-cli is not installed (brew install whisper-cpp)'
    if not os.path.exists(model):
        return None, f'Whisper model not found at {model}'
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, 'audio.wav')
        try:
            subprocess.run([_bin('ffmpeg'), '-y', '-i', path, '-ar', '16000', '-ac', '1',
                            '-c:a', 'pcm_s16le', wav],
                           capture_output=True, timeout=120, check=True)
            out = subprocess.run([cli, '-m', model, '-f', wav, '-nt', '-np'],
                                 capture_output=True, text=True, timeout=TRANSCRIBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None, 'transcription timed out'
        except (subprocess.CalledProcessError, OSError) as e:
            return None, f'audio conversion/transcription failed ({e.__class__.__name__})'
    text = ' '.join(out.stdout.split())
    if out.returncode != 0 or not text:
        return None, 'no speech recognised'
    if re.fullmatch(r'\[[^\]]*\]|\([^)]*\)', text):   # e.g. [BLANK_AUDIO]
        return None, 'no speech recognised'
    return text[:MAX_TRANSCRIPT_CHARS], None


def cleanup(project_dir, max_age_hours=MAX_AGE_HOURS):
    """Delete inbox files older than `max_age_hours`. Returns how many were deleted."""
    inbox = os.path.join(project_dir, INBOX_DIR)
    if not os.path.isdir(inbox):
        return 0
    cutoff = time.time() - max_age_hours * 3600
    deleted = 0
    for name in os.listdir(inbox):
        path = os.path.join(inbox, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                deleted += 1
        except OSError:
            pass
    return deleted


def prepare(rowid, atts, project_dir):
    """Copy attachments into the project's inbox folder and describe them.

    Returns (text_block, notes). `text_block` is appended to the user's message for
    Claude; `notes` are short lines worth texting back to the user (transcripts, skips).
    """
    inbox = os.path.join(project_dir, INBOX_DIR)
    lines, notes = [], []
    for n, att in enumerate(atts, 1):
        label = att['name'] or os.path.basename(att['path']) or f'attachment {n}'
        if not is_ready(att):
            lines.append(f'- {label}: could not be read (file missing or still downloading)')
            notes.append(f"Couldn't read attachment {label}.")
            continue
        if os.path.getsize(att['path']) > MAX_BYTES:
            lines.append(f'- {label}: skipped, larger than {MAX_BYTES // 1024 // 1024} MB')
            notes.append(f'Skipped {label}: too large.')
            continue
        what = kind(att)
        if what == 'video':
            lines.append(f'- {label}: a video; videos are not supported, so it was not saved')
            notes.append(f"Videos aren't supported, skipped {label}.")
            continue
        os.makedirs(inbox, exist_ok=True)
        dest = os.path.join(inbox, f'{rowid}-{n}-{_safe_name(label)}')
        shutil.copyfile(att['path'], dest)
        ext = os.path.splitext(dest)[1].lower()
        if what == 'image' and ext in CONVERT_EXTS:
            jpeg = os.path.splitext(dest)[0] + '.jpg'
            done = subprocess.run(['/usr/bin/sips', '-s', 'format', 'jpeg', '-Z', '2000',
                                   dest, '--out', jpeg], capture_output=True)
            if done.returncode == 0 and os.path.exists(jpeg):
                os.remove(dest)
                dest = jpeg
        rel = os.path.relpath(dest, project_dir)
        if what == 'image':
            lines.append(f'- Photo: {rel} (look at it with the Read tool)')
        elif what == 'audio':
            text, why = transcribe(dest)
            if text:
                lines.append(f'- Audio/voice message: {rel}\n  Transcript: "{text}"')
                notes.append(f'Heard: "{text[:500]}"')
            else:
                lines.append(f'- Audio/voice message: {rel} (could not be transcribed: {why})')
                notes.append(f"Couldn't transcribe the audio: {why}.")
        else:
            lines.append(f'- File: {rel}')
    if not lines:
        return '', notes
    block = ('[The user attached the following from their phone; files are saved in the '
             f'project folder under {INBOX_DIR}/]\n' + '\n'.join(lines))
    return block, notes
