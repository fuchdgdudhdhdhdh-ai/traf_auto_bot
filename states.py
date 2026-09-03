from aiogram.fsm.state import State, StatesGroup


class LoginStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()       # ввод кода через inline-клавиатуру (кнопки)
    waiting_password = State()   # 2FA пароль, если включен


class SetMessageStates(StatesGroup):
    waiting_message = State()    # ждём сообщение-образец (текст+форматирование+эмодзи)


class AddGroupStates(StatesGroup):
    waiting_link = State()


class AddSubscriberStates(StatesGroup):
    waiting_contact = State()    # @username или числовой id, вручную в базу рассылки
