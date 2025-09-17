from aiogram import Router, F
from aiogram.types import Message, FSInputFile
from keyboards import video_kb, main_kb, aspect_kb
from models import ModelName, ASPECT_CHOICES, SPEED_CHOICES
from services.moderation import check_text
from db import connect, ensure_user, create_job, set_job_status, get_job, _prepare
from texts import HELP
from config import settings
import logging
import asyncio
import os
import tempfile

from services.providers import luma as luma_api
from services.providers import veo as veo_api

logger = logging.getLogger(__name__)
router = Router()

# Простое состояние на словах
STATE = {}


@router.message(F.text == "Работа с видео")
async def video_menu(msg: Message):
    await msg.answer("Выбери модель: Veo-3 или Luma", reply_markup=video_kb())


@router.message(F.text.in_(["Veo-3", "Luma"]))
async def pick_model(msg: Message):
    STATE[msg.from_user.id] = {"model": ModelName.VEO if msg.text=="Veo-3" else ModelName.LUMA}
    await msg.answer("Пришли промпт для видео", reply_markup=main_kb())


@router.message()
async def flow(msg: Message):
    s = STATE.get(msg.from_user.id)
    if not s or "model" not in s:
        return  # пропускаем нерелевантные сообщения
    
    if "prompt" not in s:
        s["prompt"] = msg.text.strip()
        mod = check_text(s["prompt"])
        if not mod.allow:
            await msg.answer(f"🚫 Нельзя: {mod.reason}")
            STATE.pop(msg.from_user.id, None)
            return
        await msg.answer("Выбери соотношение сторон", reply_markup=aspect_kb())
        return
    
    if "aspect" not in s and msg.text in ASPECT_CHOICES:
        s["aspect"] = msg.text
        s["speed"] = SPEED_CHOICES[0]  # Iteration A: фиксируем Fast

        async with connect() as db:
            await _prepare(db)
            user = await ensure_user(db, msg.from_user.id, msg.from_user.username, 0)
            job_id = await create_job(db, user["id"], s["model"], s["aspect"], s["prompt"])

        # отправляем в провайдера
        try:
            if s["model"] == ModelName.LUMA:
                logger.info("Submitting job to luma API")
                submit = await luma_api.submit(s["prompt"], s["aspect"], s["speed"])
                provider_job_id = submit["job_id"]
                status_msg = await msg.answer(
                    "⏳ Задача отправлена на генерацию.\n"
                    f"Статус: dreaming\n"
                    f"Модель: {s['model']}, Aspect: {s['aspect']}, Speed: {s['speed']}\n"
                    f"Промпт: {s['prompt']}"
                )
                async with connect() as db:
                    await _prepare(db)
                    await set_job_status(db, job_id, "dreaming")
                
                # цикл опроса
                result = await luma_api.wait_until_complete(
                    provider_job_id,
                    interval_sec=settings.JOB_POLL_INTERVAL_SEC,
                    timeout_sec=settings.JOB_MAX_WAIT_MIN * 60,
                )
                final = result["final"]
                if final == "completed":
                    video_bytes = await luma_api.download_video(result["video_url"])
                    # временный файл для send_video
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
                        f.write(video_bytes)
                        tmp_path = f.name
                    await msg.answer_video(
                        video=FSInputFile(tmp_path), 
                        caption="✅ Готово"
                    )
                    os.unlink(tmp_path)
                    async with connect() as db:
                        await _prepare(db)
                        await set_job_status(db, job_id, "done")
                elif final == "timeout":
                    await msg.answer(
                        "⚠️ Luma долго молчит. Я продолжу пытаться позже, "
                        "а пока — попробуй другой промпт/аспект."
                    )
                    async with connect() as db:
                        await _prepare(db)
                        await set_job_status(db, job_id, "failed")
                else:
                    await msg.answer("❌ Провайдер вернул ошибку при генерации видео.")
                    async with connect() as db:
                        await _prepare(db)
                        await set_job_status(db, job_id, "failed")

            else:
                # Для Veo пока оставим заглушку, чтобы не мешать отладке Luma
                async with connect() as db:
                    await _prepare(db)
                    await set_job_status(db, job_id, "done")
                await msg.answer("✅ (Veo демо) Задача выполнена — здесь будет видеофайл.")
        except Exception as e:
            logger.exception("Error while processing video generation: %s", e)
            await msg.answer(f"❌ Произошла ошибка при генерации видео.\nДетали: {e}")
            async with connect() as db:
                await _prepare(db)
                await set_job_status(db, job_id, "failed")

        STATE.pop(msg.from_user.id, None)