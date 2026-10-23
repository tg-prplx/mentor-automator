"""Only predefined explanations may reach storage, Telegram and diagnostic reports."""

from playwright.async_api import TimeoutError as BrowserTimeout

STAGES = {
    "IDLE": "ожидание",
    "OPENING_QUIZ": "открытие страницы теста",
    "LOCATING_START": "поиск кнопки начала или продолжения",
    "OPENING_PREFLIGHT": "открытие подтверждения начала",
    "CONFIRMING_START": "подтверждение начала попытки",
    "WAITING_FOR_ATTEMPT": "ожидание страницы попытки",
    "READING_QUESTIONS": "чтение вопросов",
    "READING_IMAGE": "загрузка изображения вопроса",
    "MANUAL_INPUT": "ожидание ручного ответа в браузере",
    "MODEL": "ожидание модели",
    "APPLYING_ANSWER": "выбор и проверка ответа",
    "NEXT_PAGE": "переход к следующей странице",
    "FINAL_CONFIRMATION": "ожидание разрешения на завершение",
    "SUBMITTING": "отправка после подтверждения",
    "PREPARING_FINISH": "сохранение последней страницы и переход к завершению",
    "SUMMARY_READY": "поиск формы окончательной отправки",
    "CONFIRMING_FINISH": "подтверждение отправки в окне Moodle",
    "VERIFYING_FINISH": "проверка результата отправки",
    "RESTORING_FINAL_WAIT": "восстановление ожидания отправки без изменения ответов",
}

ERRORS = {
    "START_NOT_FOUND": "На странице не найдена видимая кнопка начала или продолжения.",
    "PREFLIGHT_NOT_READY": "Окно подтверждения начала не появилось вовремя.",
    "PREFLIGHT_REQUIRES_MANUAL_INPUT": "Перед началом Moodle требует ручного ввода.",
    "ATTEMPT_LOAD_TIMEOUT": "После нажатия не открылась страница попытки. Новая попытка автоматически не создаётся.",
    "NO_EXISTING_ATTEMPT": "Сохранённая попытка не найдена. Новую можно начать только кнопкой «Пройти».",
    "ATTEMPT_NOT_FOUND": "Страница попытки не содержит распознаваемого ID.",
    "QUESTIONS_NOT_FOUND": "На странице не найдены контейнеры вопросов.",
    "QUESTION_TEXT_NOT_FOUND": "Не найден текст вопроса.",
    "STABLE_QUESTION_ID_NOT_FOUND": "Не удалось определить стабильный ID вопроса.",
    "QUESTION_CHANGED": "Вопрос или варианты изменились. Подготовленный ответ не применён.",
    "QUESTION_IMAGE_NOT_READY": "Изображение вопроса не загрузилось. Проверьте его в браузере и возобновите выполнение.",
    "ORDER_RESPONSE_NOT_SAVED": "Порядок в интерфейсе изменился, но поле ответа не подтвердило сохранение.",
    "DRAG_SOURCE_NOT_FOUND": "Не найден доступный элемент для перетаскивания. Требуется ручной ответ.",
    "MARKER_RESET_FAILED": "Не удалось очистить прежние позиции маркеров обычными действиями браузера.",
    "MARKER_POSITION_NOT_SAVED": "Moodle не подтвердил выбранные позиции маркеров.",
    "ANSWER_NOT_APPLIED": "Интерфейс не подтвердил изменение ответа.",
    "ANSWER_VERIFICATION_FAILED": "Выбранные варианты не совпали с запрошенными.",
    "MODEL_FAILED": "Модель не вернула корректный ответ. Проверьте вход Codex и лимиты.",
    "SAFE_IMAGE_REGION_NOT_FOUND": "Не определена безопасная область изображения вопроса.",
    "SESSION_EXPIRED": "Сессия Moodle истекла. Нужен повторный вход.",
    "PAGE_DID_NOT_ADVANCE": "Moodle не переключил страницу вопросов.",
    "SAVE_REQUEST_BLOCKED": "Защита отправки заблокировала запрос сохранения. Переход остановлен; новая попытка не создаётся.",
    "ATTEMPT_ALREADY_CLOSED": "Moodle уже закрыл попытку. Продолжить её нельзя.",
    "NAVIGATION_LOOP": "Обнаружен повторный переход на ту же страницу.",
    "BROWSER_CLOSED": "Окно браузера закрыто. Перезапустите приложение и восстановите попытку.",
    "BROWSER_TIMEOUT": "Истекло время ожидания элемента браузера.",
    "BROWSER_ACTION_FAILED": "Действие браузера не выполнено. Подробности с секретными значениями скрыты.",
    "FINAL_NOT_AUTHORIZED": "Разрешение на завершение истекло или отозвано. Нужно новое подтверждение.",
    "FINAL_ATTEMPT_MISMATCH": "Страница завершения относится к другой попытке. Отправка остановлена.",
    "FINISH_NOT_FOUND": "Не найдена видимая кнопка перехода к завершению попытки.",
    "SUBMIT_NOT_FOUND": "Не найдена видимая форма окончательной отправки на сводной странице Moodle.",
    "FINISH_NAVIGATION_TIMEOUT": "Moodle не открыл сводку завершения за отведённое время.",
    "SUBMISSION_NOT_CONFIRMED": "После нажатия Moodle не показал завершённую попытку. Автоматического повтора нет.",
    "SUBMISSION_REQUEST_BLOCKED": "Защита завершения заблокировала запрос. Требуется проверка и новое разрешение.",
    "TELEGRAM_OFFLINE_FINAL_BLOCKED": "Потеряна связь с Telegram. Завершение остановлено.",
    "FINAL_AUTHORIZATION_REVOKED": "Разрешение на завершение отозвано. Требуется новое подтверждение.",
    "LEGACY_START_NOT_FOUND": "Предыдущая версия не находила видимую кнопку «Спроба тесту». Используйте кнопку повторного запуска.",
}


def safe_error(error, browser=None):
    candidate = str(error).partition(":")[0]
    if candidate in ERRORS:
        return candidate
    if browser and browser.page.is_closed():
        return "BROWSER_CLOSED"
    if isinstance(error, BrowserTimeout):
        return "BROWSER_TIMEOUT"
    return "BROWSER_ACTION_FAILED"
