import yt_dlp
import os
import csv
import random, time
import boto3
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import tempfile
import threading
from collections import defaultdict
import glob

LOG_FILE = "download_log.csv"

# S3 Configuration
S3_BUCKET = os.getenv("S3_BUCKET")
S3_FOLDER = os.getenv("S3_FOLDER")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION")

# API Configuration
API_BASE_URL = os.getenv("API_BASE_URL")

# Global progress tracking
class ProgressTracker:
    def __init__(self, total_videos):
        self.total_videos = total_videos
        self.completed = 0
        self.success_count = 0
        self.error_count = 0
        self.skipped_count = 0
        self.lock = threading.Lock()
        self.start_time = datetime.now()
    
    def update(self, status):
        with self.lock:
            self.completed += 1
            if status == "success":
                self.success_count += 1
            elif status == "error":
                self.error_count += 1
            elif status == "skipped":
                self.skipped_count += 1
    
    def get_progress_string(self):
        with self.lock:
            elapsed = datetime.now() - self.start_time
            remaining = self.total_videos - self.completed
            
            progress_bar_length = 30
            completed_length = int(progress_bar_length * self.completed / self.total_videos)
            bar = "█" * completed_length + "░" * (progress_bar_length - completed_length)
            
            percentage = (self.completed / self.total_videos) * 100
            
            return (
                f"[{bar}] {self.completed}/{self.total_videos} ({percentage:.1f}%) | "
                f"✅ {self.success_count} | ⏭ {self.skipped_count} | ❌ {self.error_count} | "
                f"⏱ {str(elapsed).split('.')[0]} | 🔄 {remaining} kaldı"
            )

progress_tracker = None

def print_header():
    """Başlık yazdır"""
    print("=" * 80)
    print("🎵 YOUTUBE VIDEO DOWNLOADER & S3 UPLOADER (WAV + SUBTITLES)")
    print("=" * 80)

def print_status(message, status_type="info"):
    """Renkli status mesajları"""
    status_icons = {
        "info": "ℹ️",
        "success": "✅", 
        "error": "❌",
        "warning": "⚠️",
        "progress": "🔄",
        "skip": "⏭️"
    }
    
    icon = status_icons.get(status_type, "•")
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    if progress_tracker:
        progress = progress_tracker.get_progress_string()
        print(f"\n{progress}")
    
    print(f"[{timestamp}] {icon} {message}")

def progress_hook(d):
    """yt-dlp indirme ilerleme callback"""
    if d['status'] == 'downloading':
        percent = d.get('_percent_str', '').strip()
        speed = d.get('_speed_str', 'N/A')
        print(f"  ⏳ İndiriliyor: {percent} | Hız: {speed}", end="\r")
    elif d['status'] == 'finished':
        print(f"  ✅ İndirme tamamlandı" + " " * 20)

def log_to_csv(user, video_url, status, message=""):
    """Log dosyasına yazar"""
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "user", "video_url", "status", "message"])
        writer.writerow([datetime.now().isoformat(), user, video_url, status, message])

def check_s3_file_exists(s3_client, bucket, key):
    """S3'te dosya var mı kontrol et"""
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except:
        return False

def upload_file_to_s3(file_path, s3_key, file_type="WAV"):
    """Dosyayı S3'e yükler"""
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION
        )

        # Dosya boyutunu al
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        
        print(f"  ☁️ {file_type} S3'e yükleniyor... ({file_size_mb:.2f} MB)")

        with open(file_path, 'rb') as f:
            s3_client.upload_fileobj(f, S3_BUCKET, s3_key)

        print(f"  ✅ {file_type} S3'e yüklendi")
        return f"s3://{S3_BUCKET}/{s3_key}"
        
    except Exception as e:
        print(f"  ❌ S3 yükleme hatası ({file_type}): {e}")
        return None

def download_and_upload_video(video_url, temp_dir, video_index, total_videos):
    """Video indir (WAV + altyazılar) ve S3'e yükle"""
    time.sleep(random.uniform(1, 3))
    
    try:
        # Video bilgisini al
        print_status(f"[{video_index}/{total_videos}] Video bilgisi alınıyor...", "progress")
        
        ydl_opts_info = {'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(video_url, download=False)
            video_title = info.get('title', 'Unknown')
            channel_name = info.get('uploader', 'Unknown')
            duration = info.get('duration', 0)
        
        # Video süresi
        duration_str = f"{duration//60}:{duration%60:02d}" if duration else "N/A"
        
        print_status(f"[{video_index}/{total_videos}] 📺 {video_title[:50]}... ({duration_str}) - {channel_name}", "info")
        
        # Güvenli dosya adları
        safe_title = "".join(c if c.isalnum() or c in " -_()" else "_" for c in video_title)[:100]
        safe_channel = "".join(c if c.isalnum() or c in " -_()" else "_" for c in channel_name)[:50]
        
        # S3 yolları
        s3_wav_key = f"{S3_FOLDER}/{safe_channel}/{safe_title}.wav"
        s3_subtitle_key = f"{S3_FOLDER}/{safe_channel}/{safe_title}.srt"
        
        # S3'te var mı kontrol et
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION
        )
        
        wav_exists = check_s3_file_exists(s3_client, S3_BUCKET, s3_wav_key)
        subtitle_exists = check_s3_file_exists(s3_client, S3_BUCKET, s3_subtitle_key)
        
        if wav_exists and subtitle_exists:
            print_status(f"[{video_index}/{total_videos}] ⏭️ Zaten mevcut (WAV+SRT): {video_title[:40]}...", "skip")
            log_to_csv(safe_channel, video_url, "skipped", "exists_in_s3")
            progress_tracker.update("skipped")
            return (video_url, True, "exists", None)
        
        # Geçici dosya yolları
        output_template = os.path.join(temp_dir, f"{safe_title}.%(ext)s")
        wav_file_path = os.path.join(temp_dir, f"{safe_title}.wav")
        
        # WAV indirme ayarları
        ydl_opts_audio = {
            'format': 'bestaudio/best',
            'outtmpl': output_template,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'wav',
                'preferredquality': '192',
            }],
            'quiet': True,
            'noplaylist': True,
            'progress_hooks': [progress_hook],
        }
        
        # Altyazı indirme ayarları
        ydl_opts_subtitle = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['tr', 'en'],  # Türkçe ve İngilizce
            'subtitlesformat': 'srt',
            'outtmpl': output_template,
            'quiet': True,
            'noplaylist': True,
        }
        
        # 1. WAV dosyasını indir
        if not wav_exists:
            print(f"  🎵 WAV indiriliyor: {video_title[:40]}...")
            with yt_dlp.YoutubeDL(ydl_opts_audio) as ydl:
                ydl.download([video_url])
        
        # 2. Altyazıları indir
        if not subtitle_exists:
            print(f"  📝 Altyazılar indiriliyor...")
            with yt_dlp.YoutubeDL(ydl_opts_subtitle) as ydl:
                ydl.download([video_url])
        
        # S3'e yükleme
        upload_results = {}
        
        # WAV yükle
        if os.path.exists(wav_file_path) and not wav_exists:
            s3_wav_url = upload_file_to_s3(wav_file_path, s3_wav_key, "WAV")
            upload_results['wav'] = s3_wav_url
            os.remove(wav_file_path)
        elif wav_exists:
            upload_results['wav'] = f"s3://{S3_BUCKET}/{s3_wav_key}"
            print(f"  ⏭️ WAV zaten mevcut")
        
        # Altyazı dosyalarını bul ve yükle
        subtitle_pattern = os.path.join(temp_dir, f"{safe_title}*.srt")
        subtitle_files = glob.glob(subtitle_pattern)
        
        if subtitle_files and not subtitle_exists:
            # İlk bulunan altyazı dosyasını kullan (genellikle tr veya en)
            subtitle_file = subtitle_files[0]
            s3_subtitle_url = upload_file_to_s3(subtitle_file, s3_subtitle_key, "SRT")
            upload_results['subtitle'] = s3_subtitle_url
            
            # Tüm altyazı dosyalarını temizle
            for sf in subtitle_files:
                try:
                    os.remove(sf)
                except:
                    pass
        elif subtitle_exists:
            upload_results['subtitle'] = f"s3://{S3_BUCKET}/{s3_subtitle_key}"
            print(f"  ⏭️ Altyazı zaten mevcut")
        else:
            print(f"  ⚠️ Altyazı bulunamadı")
            upload_results['subtitle'] = None
        
        # Sonuç kontrolü
        if upload_results.get('wav'):
            status_msg = "WAV"
            if upload_results.get('subtitle'):
                status_msg += " + SRT"
            
            print_status(f"[{video_index}/{total_videos}] ✅ Başarılı ({status_msg}): {video_title[:40]}...", "success")
            log_to_csv(safe_channel, video_url, "success", json.dumps(upload_results))
            progress_tracker.update("success")
            return (video_url, True, None, upload_results)
        else:
            print_status(f"[{video_index}/{total_videos}] ❌ Yükleme hatası: {video_title[:40]}...", "error")
            log_to_csv(safe_channel, video_url, "error", "upload_failed")
            progress_tracker.update("error")
            return (video_url, False, "Upload failed", None)
            
    except Exception as e:
        print_status(f"[{video_index}/{total_videos}] ❌ Hata: {str(e)[:60]}...", "error")
        log_to_csv("unknown", video_url, "error", str(e))
        progress_tracker.update("error")
        return (video_url, False, str(e), None)

def get_video_list_from_api():
    """API'den video listesi al"""
    try:
        print_status("API'den video listesi alınıyor...", "progress")
        print_status(f"API URL: {API_BASE_URL}/get-video-list", "info")
        
        response = requests.get(f"{API_BASE_URL}/get-video-list", timeout=30)
        response.raise_for_status()
        
        data = response.json()
        print_status(f"API Response: {json.dumps(data, indent=2, ensure_ascii=False)[:200]}...", "info")
        
        status = data.get("status")
        
        if status == "success":
            video_lines = data.get("video_list", [])
            list_id = data.get("list_id")
            print_status(f"API'den {len(video_lines)} video alındı (list_id: {list_id})", "success")
            return video_lines, list_id
        elif status == "no_more_files":
            message = data.get("message", "Tüm dosyalar işlendi")
            print_status(f"📭 {message}", "warning")
            print_status(f"   Aktif işlemler: {data.get('active_processes', 0)}", "info")
            print_status(f"   İşlenen dosyalar: {data.get('processed_files', 0)}", "info")
            return [], None
        else:
            print_status(f"API'den beklenmeyen status: {status}", "error")
            print_status(f"Mesaj: {data.get('message', 'N/A')}", "error")
            return [], None
    except requests.exceptions.ConnectionError as e:
        print_status(f"API'ye bağlanılamıyor: {API_BASE_URL}", "error")
        print_status(f"Lütfen API sunucusunun çalıştığından emin olun", "error")
        print_status(f"Hata: {e}", "error")
        return [], None
    except requests.exceptions.Timeout:
        print_status(f"API zaman aşımı (30s)", "error")
        return [], None
    except Exception as e:
        print_status(f"API hatası: {e}", "error")
        return [], None

def notify_api_completion(list_id, status, message=""):
    """API'ye durum bildir"""
    if not list_id:
        return
        
    try:
        payload = {
            "list_id": list_id,
            "status": status,
            "message": message,
            "timestamp": datetime.now().isoformat()
        }
        response = requests.post(f"{API_BASE_URL}/notify-completion", json=payload, timeout=10)
        response.raise_for_status()
        print_status("API'ye durum bildirildi", "success")
    except Exception as e:
        print_status(f"API bildirim hatası: {e}", "warning")

def download_videos_from_api(max_workers=4):
    """Ana fonksiyon"""
    global progress_tracker
    
    print_header()
    
    video_lines, list_id = get_video_list_from_api()
    
    if not video_lines:
        print_status("Video listesi alınamadı - çıkılıyor", "error")
        return

    # URL'leri çıkar
    video_urls = []
    for line in video_lines:
        if isinstance(line, dict):
            video_url = line.get('video_url', '')
        else:
            line = line.strip()
            if line.startswith('https://') or line.startswith('http://'):
                video_url = line
            else:
                parts = line.split('|')
                video_url = parts[1].strip() if len(parts) >= 2 else ''
        
        if video_url:
            video_urls.append(video_url)

    if not video_urls:
        print_status("Geçerli URL bulunamadı", "error")
        return

    total_videos = len(video_urls)
    progress_tracker = ProgressTracker(total_videos)
    
    print_status(f"Toplam {total_videos} video işlenecek", "info")
    print_status(f"Maksimum {max_workers} thread kullanılacak", "info")
    print_status("WAV + Altyazı indirme aktif", "info")
    print_status("İşlem başlatılıyor...", "progress")
    print("-" * 80)

    # Geçici klasör
    temp_dir = tempfile.mkdtemp(prefix="yt_")
    print_status(f"Geçici klasör: {temp_dir}", "info")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Video URL'lerini index ile beraber gönder
        futures = [
            executor.submit(download_and_upload_video, url, temp_dir, i+1, total_videos)
            for i, url in enumerate(video_urls)
        ]
        
        for future in as_completed(futures):
            video_url, success, error, s3_data = future.result()

    # Temizlik
    try:
        import shutil
        shutil.rmtree(temp_dir)
        print_status("Geçici dosyalar temizlendi", "info")
    except Exception as e:
        print_status(f"Temizlik hatası: {e}", "warning")

    # Final özet
    print("\n" + "=" * 80)
    print("🎉 İŞLEM TAMAMLANDI!")
    print("=" * 80)
    
    elapsed_total = datetime.now() - progress_tracker.start_time
    print(f"⏱️  Toplam süre: {str(elapsed_total).split('.')[0]}")
    print(f"📊 Toplam video: {total_videos}")
    print(f"✅ Başarılı: {progress_tracker.success_count}")
    print(f"⏭️  Zaten mevcut: {progress_tracker.skipped_count}")
    print(f"❌ Hatalı: {progress_tracker.error_count}")
    
    success_rate = (progress_tracker.success_count / total_videos) * 100 if total_videos > 0 else 0
    print(f"📈 Başarı oranı: {success_rate:.1f}%")

    # API'ye bildir
    message = f"Processed: {progress_tracker.success_count} new, {progress_tracker.skipped_count} existing, {progress_tracker.error_count} errors (WAV+SRT)"
    final_status = "completed" if progress_tracker.error_count == 0 else "partial"
    notify_api_completion(list_id, final_status, message)
    
    print("=" * 80)

if __name__ == "__main__":
    download_videos_from_api(max_workers=8)
