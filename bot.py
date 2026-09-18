@bot.on(events.CallbackQuery)
async def process_task_callback(event):
    if not await is_allowed(event):
        await event.answer("⚠️ ليس لديك صلاحية استخدام هذا البوت.", alert=True)
        return

    data = (event.data or b"").decode("utf-8", "ignore")
    if not data:
        return

    prefixes = (
        "q_1080_", "q_720_", "q_480_", "q_mp3_",
        "prof_img_", "prof_doc_", "tt_photo_", "tt_doc_"
    )
    action = next((p for p in prefixes if data.startswith(p)), None)
    if not action:
        return

    task_key = data[len(action):]
    if not task_key:
        await event.answer("⚠️ بيانات الطلب غير صالحة.", alert=True)
        return

    url, task_type = await pop_task(task_key)
    if not url:
        await event.answer("⚠️ انتهت صلاحية هذا الزر أو تم تنفيذه بالفعل.", alert=True)
        return

    await event.delete()
    user_config = await get_user_config(event.chat_id)

    if action.startswith("prof_"):
        as_doc = action == "prof_doc_"
        status_msg = await bot.send_message(event.chat_id, "⏳ **جاري معالجة طلب الصورة الشخصية...**")
        asyncio.create_task(
            start_direct_execution(
                chat_id=event.chat_id,
                url=url,
                filename="profile_avatar.jpg",
                as_doc=as_doc,
                status_msg=status_msg,
                is_avatar_task=True
            )
        )
    elif action.startswith("tt_"):
        as_doc = action == "tt_doc_"
        status_msg = await bot.send_message(event.chat_id, "⏳ **جاري تنزيل وسائط منشور TikTok...**")
        asyncio.create_task(
            start_direct_execution(
                chat_id=event.chat_id,
                url=url,
                filename="tiktok_media",
                as_doc=as_doc,
                status_msg=status_msg
            )
        )
    elif task_type == "dailymotion":
        quality_map = {
            "q_1080_": "1080",
            "q_720_": "720",
            "q_480_": "480",
            "q_mp3_": "mp3"
        }
        selected_q = quality_map.get(action, "720")
        status_msg = await bot.send_message(event.chat_id, "⏳ **جاري معالجة طلب Dailymotion...**")
        asyncio.create_task(
            download_dailymotion_video(event, url, selected_q, status_msg)
        )
    else:
        quality_map = {
            "q_1080_": "1080",
            "q_720_": "720",
            "q_480_": "480",
            "q_mp3_": "mp3"
        }
        selected_q = quality_map.get(action, user_config["quality"])
        target_fmt = "mp3" if action == "q_mp3_" else "mp4"

        status_msg = await bot.send_message(event.chat_id, "⏳ **جاري تحضير وبدء تنزيل المحتوى...**")
        asyncio.create_task(
            start_direct_execution(
                chat_id=event.chat_id,
                url=url,
                filename=get_clean_filename(url),
                as_doc=False,
                quality=selected_q,
                target_fmt=target_fmt,
                status_msg=status_msg
            )
        )

# --- تشغيل البوت مع تهيئة قاعدة البيانات ---
async def main():
    await init_db()
    print(f"🤖 جاري تشغيل البوت بنجاح الاصدار {VERSION}...")
    await bot.start(bot_token=BOT_TOKEN)
    await bot.run_until_disconnected()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
