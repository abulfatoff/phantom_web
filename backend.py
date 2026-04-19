import os
import sys
import json
import threading
import queue
import shlex
import shutil
import re
import asyncio
import tempfile
import uuid
from typing import Dict, Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

APP_NAME = "PHANTOM"
CONFIG_FILE = "config.json"

log_queue = queue.Queue()
active_downloads = 0

def is_ffmpeg_available() -> bool:
    """Check if ffmpeg is available system-wide (Docker) or locally."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True
    if os.path.isfile("ffmpeg.exe") and os.path.isfile("ffprobe.exe"):
        return True
    if os.path.isfile("ffmpeg") and os.path.isfile("ffprobe"):
        return True
    return False

class MyLogger:
    def __init__(self, log_queue: queue.Queue):
        self.log_queue = log_queue

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self.log_queue.put({"type": "log", "msg": f"{msg}\n"})

    def info(self, msg):
        self.log_queue.put({"type": "log", "msg": f"{msg}\n"})

    def warning(self, msg):
        self.log_queue.put({"type": "log", "msg": f"[yt-dlp UYARI]: {msg}\n"})

    def error(self, msg):
        self.log_queue.put({"type": "log", "msg": f"[yt-dlp ERROR]: {msg}\n"})

B_I18N = {
    "TR": {
        "start": "[SİSTEM] İndirme modülü aktif ediliyor...",
        "url": "[SİSTEM] Hedef Bağlantı",
        "wait": "[SİSTEM] Video meta verileri analiz ediliyor...",
        "success": "[BAŞARI] İşlem başarıyla tamamlandı!",
        "fail_cmd": "[HATA] Özel Komut Okunamadı",
        "override": "[AYAR] Manuel Parametre Tespit Edildi",
        "err": "[HATA]"
    },
    "AZ": {
        "start": "[SİSTEM] Yükləmə modulu aktivləşdirilir...",
        "url": "[SİSTEM] Hədəf Keçid",
        "wait": "[SİSTEM] Video meta məlumatları analiz edilir...",
        "success": "[UĞUR] Proses uğurla başa çatdı!",
        "fail_cmd": "[XƏTA] Xüsusi Əmr Oxuna Bilmədi",
        "override": "[AYAR] Manuel Parametr Təsbit Edildi",
        "err": "[XƏTA]"
    },
    "EN": {
        "start": "[SYSTEM] Download engine initializing...",
        "url": "[SYSTEM] Target URL",
        "wait": "[SYSTEM] Analyzing video metadata...",
        "success": "[SUCCESS] Operation completed successfully!",
        "fail_cmd": "[ERROR] Failed to Parse Custom Command",
        "override": "[FLAG] Manual Parameter Detected",
        "err": "[ERROR]"
    }
}

HISTORY_FILE = "history.json"

class HistoryManager:
    @staticmethod
    def load_history() -> list:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    @staticmethod
    def add_history(title: str, url: str, format_str: str):
        import datetime
        data = HistoryManager.load_history()
        entry = {
            "title": title,
            "url": url,
            "format": format_str,
            "date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        data.insert(0, entry)
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data[:100], f, indent=4, ensure_ascii=False)
        except Exception:
            pass

class ConfigManager:
    @staticmethod
    def load_config() -> dict:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "remember": True,
            "format": 0,
            "custom_commands": "",
            "theme": "System",
            "language": "TR",
            "custom_templates": {}
        }

    @staticmethod
    def save_config(config_data: dict):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save config: {e}")

class CommandParser:
    @staticmethod
    def parse_to_ydl_opts(raw_text: str) -> Dict[str, Any]:
        import yt_dlp
        opts: Dict[str, Any] = {}
        if not raw_text.strip():
            return opts
        try:
            args = shlex.split(raw_text)
        except ValueError as e:
            raise ValueError(f"Komut ayrıştırma hatası: {e}")
        i = 0
        formats_found = []
        while i < len(args):
            arg = args[i]
            if arg in ('-f', '--format') and i + 1 < len(args):
                formats_found.append(args[i + 1])
                i += 1
            elif arg == '--embed-subs':
                opts['writesubtitles'] = True
                opts['subtitleslangs'] = ['all']
            elif arg in ('--write-auto-subs', '--write-auto-sub'):
                opts['writeautomaticsub'] = True
            elif arg == '--extract-audio' or arg == '-x':
                opts['extractaudio'] = True
            elif arg == '--audio-format' and i + 1 < len(args):
                opts['audioformat'] = args[i + 1]
                i += 1
            elif arg == '--audio-quality' and i + 1 < len(args):
                opts['audioquality'] = args[i + 1]
                i += 1
            elif arg == '--download-section' and i + 1 < len(args):
                val = args[i+1]
                opts['download_ranges'] = yt_dlp.utils.download_range_func(None, [[val]])
                i += 1
            elif arg in ('--merge-output-format'):
                if i + 1 < len(args):
                    opts['merge_output_format'] = args[i + 1]
                    i += 1
            elif arg == '--embed-thumbnail':
                 opts['writethumbnail'] = True
                 opts.setdefault('postprocessors', []).append({'key': 'EmbedThumbnail'})
            elif arg == '--proxy' and i + 1 < len(args):
                 opts['proxy'] = args[i + 1]
                 i += 1
            i += 1
        if formats_found:
            opts['format'] = formats_found[-1] 
        return opts


class YTDLPWorker:
    def __init__(self, log_queue: queue.Queue):
        self.log_queue = log_queue

    def download_hook(self, d):
        import yt_dlp
        if d['status'] == 'downloading':
            percent_str = yt_dlp.utils.remove_quotes(d.get('_percent_str', '0%'))
            percent_clean = re.sub(r'\x1b[^m]*m', '', percent_str).strip()
            try:
                numeric_val = float(percent_clean.replace('%', '')) / 100.0
            except ValueError:
                numeric_val = 0.0

            speed = d.get('_speed_str', 'N/A')
            eta = d.get('_eta_str', 'N/A')
            msg = f"> [yt-dlp] {percent_clean} | İndirme Hızı: {speed} | Kalan Süre: {eta}"
            self.log_queue.put({"type": "progress", "val": numeric_val, "msg": msg})

        elif d['status'] == 'finished':
            self.log_queue.put({"type": "log", "msg": "\n[yt-dlp] İndirme tamamlandı. Dosya işleniyor (Muxing/Dönüştürme)...\n"})

    def execute_download(self, url: str, format_id: str, download_type: str, start_time: str, end_time: str, custom_cmd: str) -> Optional[str]:
        import yt_dlp

        cfg = ConfigManager.load_config()
        lang = cfg.get("language", "EN")
        if lang not in B_I18N: lang = "EN"
        t = B_I18N[lang]

        self.log_queue.put({"type": "log", "msg": f"{t['start']}\n{t['url']}: {url}\n"})
        
        # Cloud: Always download to /tmp with UUID prefix
        uid = str(uuid.uuid4())[:8]
        abs_dir = tempfile.gettempdir()
        temp_out = os.path.join(abs_dir, f"phantom_{uid}_%(title)s.%(ext)s")
        
        # Detect ffmpeg location: system PATH first, then local directory
        ffmpeg_loc = None
        if shutil.which("ffmpeg"):
            ffmpeg_loc = os.path.dirname(shutil.which("ffmpeg"))
        elif os.path.isfile("ffmpeg") or os.path.isfile("ffmpeg.exe"):
            ffmpeg_loc = '.'
        
        ydl_opts: Dict[str, Any] = {
            'logger': MyLogger(self.log_queue),
            'progress_hooks': [self.download_hook],
            'outtmpl': temp_out,
            'noplaylist': True,
        }
        
        if ffmpeg_loc:
            ydl_opts['ffmpeg_location'] = ffmpeg_loc

        # Format Logic
        if download_type == 'audio':
             ydl_opts['format'] = 'bestaudio/best'
             ydl_opts['postprocessors'] = [{
                 'key': 'FFmpegExtractAudio',
                 'preferredcodec': 'mp3',
                 'preferredquality': '320',
             }]
        else:
             if format_id == "Auto":
                 target_fmt = "bestvideo+bestaudio/best"
             else:
                 res = format_id.replace('p', '')
                 target_fmt = f"bestvideo[height<={res}]+bestaudio/best"
             ydl_opts['format'] = target_fmt
             ydl_opts['merge_output_format'] = 'mp4'

        # Bölgesel İndirme Güvenli Tetikleyicisi
        if start_time or end_time:
             import yt_dlp.utils
             s_time = yt_dlp.utils.parse_duration(start_time) if start_time else 0
             e_time = yt_dlp.utils.parse_duration(end_time) if end_time else 999999
             ydl_opts['download_ranges'] = lambda info_dict, ydl: [{'start_time': s_time, 'end_time': e_time}]

        try:
            custom_opts = CommandParser.parse_to_ydl_opts(custom_cmd)
            for k, v in custom_opts.items():
                ydl_opts[k] = v 
                val_repr = "Timestamp Interval" if k == "download_ranges" else v
                self.log_queue.put({"type": "log", "msg": f"{t['override']}: {k} = {val_repr}\n"})
        except Exception as e:
            self.log_queue.put({"type": "log", "msg": f"{t['fail_cmd']}: {str(e)}\n"})
            return None

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self.log_queue.put({"type": "log", "msg": f"{t['wait']}\n"})
                info = ydl.extract_info(url, download=True)
                
                if info:
                    final_filename = ydl.prepare_filename(info)
                    
                    if download_type == 'audio':
                        base, _ = os.path.splitext(final_filename)
                        final_filename = f"{base}.mp3"
                    elif 'merge_output_format' in ydl_opts:
                         base, _ = os.path.splitext(final_filename)
                         final_filename = f"{base}.{ydl_opts['merge_output_format']}"

                    if os.path.exists(final_filename):
                        # History
                        title = info.get('title', 'Unknown')
                        HistoryManager.add_history(title, url, format_id if not start_time else f"{format_id} (Partial)")
                        self.log_queue.put({"type": "log", "msg": f"{t['success']}\n"})
                        return final_filename

        except Exception as e:
            self.log_queue.put({"type": "log", "msg": f"\n{t['err']}: {str(e)}\n"})
            
        return None

# ─── API ROUTE MODELS ─────────────────────────────────────────────────
class DownloadRequest(BaseModel):
    url: str
    format_id: str
    download_type: str = "video"
    start_time: str = ""
    end_time: str = ""
    custom_commands: str = ""

class SettingsRequest(BaseModel):
    remember: bool = True
    format: int = 0
    custom_commands: str = ""
    theme: str = "System"
    language: str = "TR"
    custom_templates: Dict[str, str] = {}

class InfoRequest(BaseModel):
    url: str

# ─── API ROUTES ───────────────────────────────────────────────────────
@app.post("/api/info")
def extract_video_info(req: InfoRequest):
    def fetch():
        import yt_dlp
        ydl_opts = {'noplaylist': True, 'quiet': True}
        ffmpeg_loc = None
        if shutil.which("ffmpeg"):
            ffmpeg_loc = os.path.dirname(shutil.which("ffmpeg"))
        elif os.path.isfile("ffmpeg") or os.path.isfile("ffmpeg.exe"):
            ffmpeg_loc = '.'
        if ffmpeg_loc:
            ydl_opts['ffmpeg_location'] = ffmpeg_loc
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(req.url, download=False)
                
                formats = []
                if 'formats' in info:
                    res_set = set()
                    for f in info['formats']:
                        if f.get('vcodec') != 'none' and f.get('height'):
                            h = f.get('height')
                            if h not in res_set:
                                res_set.add(h)
                    sorted_res = sorted(list(res_set), reverse=True)
                    formats = [f"{h}p" for h in sorted_res if h in [2160, 1440, 1080, 720, 480, 360]]
                
                return {
                    "title": info.get('title', 'Açıklama veya başlık bulunamadı.'),
                    "thumbnail": info.get('thumbnail', ''),
                    "formats": formats if formats else ["Auto"]
                }
        except Exception as e:
            return {"error": str(e)}
            
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(fetch)
        res = future.result()
        if "error" in res:
             return JSONResponse(status_code=400, content={"error": res["error"]})
        return res

@app.get("/api/settings")
def get_settings():
    config_data = ConfigManager.load_config()
    return {"settings": config_data, "ffmpeg_available": is_ffmpeg_available()}

@app.get("/api/history")
def get_history():
    return {"history": HistoryManager.load_history()}

@app.post("/api/settings")
def save_settings(settings: SettingsRequest):
    ConfigManager.save_config(settings.dict())
    return {"status": "ok"}

# ─── CLOUD DOWNLOAD: FileResponse + BackgroundTasks cleanup ──────────
def cleanup_temp_file(path: str):
    """Delete temp file from server after it has been sent to client."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
            print(f"[CLEANUP] Temp file removed: {path}")
    except Exception as e:
        print(f"[CLEANUP] Failed to remove temp file: {e}")

@app.post("/api/download")
async def trigger_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    global active_downloads
    if active_downloads > 0:
        return JSONResponse(status_code=400, content={"error": "Zaten aktif bir indirme veya işlem var."})
    
    if not req.url:
         return JSONResponse(status_code=400, content={"error": "Medya Bağlantısı (URL) boş olamaz."})
    
    active_downloads += 1
    worker = YTDLPWorker(log_queue)
    
    try:
        file_path = await asyncio.to_thread(
            worker.execute_download, 
            req.url, req.format_id, req.download_type, req.start_time, req.end_time, req.custom_commands
        )
        
        if file_path and os.path.exists(file_path):
            # Extract a clean download name (strip "phantom_{uid}_" prefix)
            filename = os.path.basename(file_path)
            parts = filename.split("_", 2)
            if len(parts) >= 3 and parts[0] == "phantom":
                clean_name = parts[2]
            else:
                clean_name = filename
                
            log_queue.put({"type": "log", "msg": f"[BİLGİ] Dosya tarayıcıya aktarılıyor...\n"})
            log_queue.put({"type": "done"})
            
            # Schedule temp file deletion AFTER response is sent
            background_tasks.add_task(cleanup_temp_file, file_path)
            
            active_downloads = 0
            return FileResponse(
                path=file_path, 
                filename=clean_name, 
                media_type='application/octet-stream'
            )
        else:
            log_queue.put({"type": "done"})
            active_downloads = 0
            return JSONResponse(status_code=500, content={"error": "İndirme başarısız oldu veya dosya bulunamadı."})
            
    except Exception as e:
        log_queue.put({"type": "done"})
        active_downloads = 0
        return JSONResponse(status_code=500, content={"error": str(e)})

# ─── WEBSOCKET: Live Terminal Log Stream ──────────────────────────────
@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            try:
                msg = log_queue.get_nowait()
                await websocket.send_json(msg)
            except queue.Empty:
                await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass

# ─── Static File Serving (Frontend) ──────────────────────────────────
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), 'frontend', 'dist')
if not os.path.exists(FRONTEND_DIR):
    os.makedirs(FRONTEND_DIR, exist_ok=True)
    with open(os.path.join(FRONTEND_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write("<h1>UI Eksik / The Frontend UI is Missing.</h1>")

app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
