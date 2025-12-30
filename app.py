import os
import uuid
import tempfile
import asyncio
from datetime import timedelta
import warnings
from typing import Optional
import whisper
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
from telegram.constants import ParseMode

warnings.filterwarnings("ignore")

# States for conversation
CHOOSING_LANGUAGE, UPLOADING_VIDEO = range(2)

# Available languages
LANGUAGES = {
    'en': 'English',
    'km': 'Khmer'
}

class VideoToSRTBot:
    def __init__(self, token: str):
        self.token = token
        self.upload_folder = 'uploads'
        os.makedirs(self.upload_folder, exist_ok=True)
        
        # Allowed file extensions
        self.allowed_extensions = {
            'mp4', 'avi', 'mov', 'mkv', 'wmv', 
            'flv', 'webm', 'm4v', 'mpg', 'mpeg'
        }
        
        # User session data: {user_id: {'language': 'en', 'filename': '...'}}
        self.user_sessions = {}
    
    def allowed_file(self, filename: str) -> bool:
        """Check if file extension is allowed"""
        return '.' in filename and \
               filename.rsplit('.', 1)[1].lower() in self.allowed_extensions
    
    def time_format(self, seconds: float) -> str:
        """Convert seconds to SRT time format"""
        td = timedelta(seconds=seconds)
        hours = td.seconds // 3600
        minutes = (td.seconds % 3600) // 60
        seconds = td.seconds % 60
        milliseconds = td.microseconds // 1000
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
    
    def transcribe_video(self, video_path: str, language: str):
        """
        Transcribe video using Whisper model
        """
        # Map language for Whisper
        whisper_lang = 'en' if language == 'en' else 'km'
        
        # Load Whisper model
        model = whisper.load_model("small")
        
        # Transcribe with word-level timestamps
        result = model.transcribe(
            video_path,
            language=whisper_lang,
            word_timestamps=True,
            verbose=False,
            fp16=False
        )
        
        # Get segments
        segments = result["segments"]
        
        # Refine timing
        for segment in segments:
            if segment['start'] < 0:
                segment['start'] = 0
            if segment['end'] <= segment['start']:
                segment['end'] = segment['start'] + 1
        
        return segments
    
    def create_srt(self, segments, output_path: str) -> str:
        """Create SRT file from transcription segments"""
        with open(output_path, 'w', encoding='utf-8') as f:
            for i, segment in enumerate(segments, 1):
                start_time = self.time_format(segment['start'])
                end_time = self.time_format(segment['end'])
                text = segment['text'].strip()
                
                # Clean up text
                text = text.replace('...', '…').replace('..', '.')
                
                f.write(f"{i}\n")
                f.write(f"{start_time} --> {end_time}\n")
                f.write(f"{text}\n\n")
        
        return output_path
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a welcome message when the command /start is issued."""
        user = update.effective_user
        welcome_text = (
            f"👋 Hello {user.first_name}!\n\n"
            "I can convert your video files to SRT subtitle files with accurate timing.\n\n"
            "📁 **Supported formats:** MP4, AVI, MOV, MKV, WMV, FLV, WEBM, M4V, MPG, MPEG\n"
            "🗣️ **Supported languages:** English and Khmer\n\n"
            "To start, please use /convert command."
        )
        
        await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)
    
    async def convert_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the conversion process"""
        keyboard = [
            [
                InlineKeyboardButton("🇺🇸 English", callback_data="lang_en"),
                InlineKeyboardButton("🇰🇭 Khmer", callback_data="lang_km")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "Please choose the language of the audio in your video:",
            reply_markup=reply_markup
        )
        
        return CHOOSING_LANGUAGE
    
    async def language_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle language selection"""
        query = update.callback_query
        await query.answer()
        
        # Extract language from callback data
        language = query.data.split('_')[1]
        user_id = query.from_user.id
        
        # Store user's language choice
        self.user_sessions[user_id] = {'language': language}
        
        await query.edit_message_text(
            f"✅ Language set to: {LANGUAGES[language]}\n\n"
            "Now, please upload your video file. "
            "Max file size is 50MB (Telegram limitation)."
        )
        
        return UPLOADING_VIDEO
    
    async def handle_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle video file upload"""
        user_id = update.effective_user.id
        
        # Check if user has selected language first
        if user_id not in self.user_sessions or 'language' not in self.user_sessions[user_id]:
            await update.message.reply_text(
                "Please start the conversion process with /convert first."
            )
            return ConversationHandler.END
        
        # Check if document is a video
        if not update.message.document and not update.message.video:
            await update.message.reply_text(
                "Please send a video file. "
                "You can send it as a document or as a video."
            )
            return UPLOADING_VIDEO
        
        # Get file information
        if update.message.document:
            file = update.message.document
            mime_type = file.mime_type
        else:
            file = update.message.video
            mime_type = file.mime_type
        
        # Check file size (Telegram bot API limit is 50MB for files)
        if file.file_size > 50 * 1024 * 1024:
            await update.message.reply_text(
                "File is too large. Maximum size is 50MB."
            )
            return UPLOADING_VIDEO
        
        # Get file extension from mime type or filename
        filename = file.file_name or "video"
        if '.' not in filename:
            # Try to determine extension from mime type
            if 'mp4' in mime_type:
                filename += '.mp4'
            elif 'avi' in mime_type:
                filename += '.avi'
            elif 'mov' in mime_type:
                filename += '.mov'
            elif 'mkv' in mime_type:
                filename += '.mkv'
            else:
                filename += '.mp4'  # default
        
        # Check if file type is allowed
        if not self.allowed_file(filename):
            allowed = ', '.join(sorted(self.allowed_extensions))
            await update.message.reply_text(
                f"File type not supported. Please upload: {allowed}."
            )
            return UPLOADING_VIDEO
        
        # Send processing message
        processing_msg = await update.message.reply_text(
            "⏳ Downloading and processing your video... This may take a while."
        )
        
        try:
            # Generate unique filename
            file_id = str(uuid.uuid4())[:8]
            video_path = os.path.join(self.upload_folder, f"{file_id}_{filename}")
            
            # Download the file
            file_obj = await file.get_file()
            await file_obj.download_to_drive(video_path)
            
            # Update processing message
            await processing_msg.edit_text("🎯 Transcribing audio... This may take several minutes.")
            
            # Transcribe video
            language = self.user_sessions[user_id]['language']
            segments = await asyncio.to_thread(
                self.transcribe_video, video_path, language
            )
            
            # Create SRT file
            srt_filename = f"{os.path.splitext(filename)[0]}_{language}_synced.srt"
            srt_path = os.path.join(self.upload_folder, f"{file_id}_{srt_filename}")
            
            await asyncio.to_thread(self.create_srt, segments, srt_path)
            
            # Clean up video file
            if os.path.exists(video_path):
                os.remove(video_path)
            
            # Calculate statistics
            total_duration = segments[-1]['end'] if segments else 0
            subtitle_count = len(segments)
            
            # Update processing message
            await processing_msg.edit_text(
                f"✅ Processing complete!\n\n"
                f"📊 **Statistics:**\n"
                f"• Language: {LANGUAGES[language]}\n"
                f"• Duration: {total_duration:.1f} seconds\n"
                f"• Subtitles: {subtitle_count} segments\n"
                f"• Avg duration: {total_duration/subtitle_count:.2f}s per segment\n\n"
                f"Sending SRT file..."
            )
            
            # Send SRT file
            with open(srt_path, 'rb') as srt_file:
                await update.message.reply_document(
                    document=srt_file,
                    filename=srt_filename,
                    caption=f"Here's your SRT file for {filename}"
                )
            
            # Clean up SRT file
            if os.path.exists(srt_path):
                os.remove(srt_path)
            
            # Clean up user session
            if user_id in self.user_sessions:
                del self.user_sessions[user_id]
            
            await processing_msg.delete()
            
        except Exception as e:
            await processing_msg.edit_text(
                f"❌ Error processing video: {str(e)}"
            )
            
            # Clean up files on error
            for file_path in [video_path, srt_path]:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except:
                        pass
            
            # Clean up user session
            if user_id in self.user_sessions:
                del self.user_sessions[user_id]
        
        return ConversationHandler.END
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel the conversation"""
        user_id = update.effective_user.id
        if user_id in self.user_sessions:
            del self.user_sessions[user_id]
        
        await update.message.reply_text(
            "Conversion cancelled. You can start again with /convert"
        )
        return ConversationHandler.END
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a help message"""
        help_text = (
            "🤖 **Video to SRT Bot Help**\n\n"
            "**Commands:**\n"
            "/start - Start the bot\n"
            "/convert - Convert video to SRT subtitles\n"
            "/help - Show this help message\n"
            "/cancel - Cancel current operation\n\n"
            "**How to use:**\n"
            "1. Use /convert to start\n"
            "2. Choose the audio language\n"
            "3. Upload your video file\n"
            "4. Wait for processing\n"
            "5. Download your SRT file\n\n"
            "**Supported formats:** MP4, AVI, MOV, MKV, WMV, FLV, WEBM, M4V, MPG, MPEG\n"
            "**Max file size:** 50MB\n"
            "**Languages:** English, Khmer"
        )
        await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors"""
        print(f"Update {update} caused error {context.error}")
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "An error occurred. Please try again."
            )
    
    def run(self):
        """Run the bot"""
        # Create application
        application = Application.builder().token(self.token).build()
        
        # Create conversation handler for /convert command
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("convert", self.convert_command)],
            states={
                CHOOSING_LANGUAGE: [
                    CallbackQueryHandler(self.language_callback, pattern="^lang_")
                ],
                UPLOADING_VIDEO: [
                    MessageHandler(
                        filters.Document.VIDEO | filters.Document.ALL | filters.VIDEO,
                        self.handle_video
                    ),
                    CommandHandler("cancel", self.cancel)
                ]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            allow_reentry=True
        )
        
        # Add handlers
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(conv_handler)
        application.add_error_handler(self.error_handler)
        
        # Start the bot
        print("Bot is starting...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    # Get your bot token from @BotFather
    BOT_TOKEN = "7638697613:AAHweT61kMgupTD3flLheNLx-DbkEDj5mtk"  # Replace with your bot token
    
    bot = VideoToSRTBot(BOT_TOKEN)
    bot.run()