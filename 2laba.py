import sys
import serial
import serial.tools.list_ports

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QComboBox, QTextEdit,
    QMessageBox, QGroupBox
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QTextCursor


# ============================================================
# ПАРАМЕТРЫ КАДРА (Вариант 1, Группа 450502)
# ============================================================

# Флаг начала кадра: 45050 + последняя цифра группы (2) + "-" + вариант (1)
FRAME_FLAG = b"450502-1"

# Фиксированная длина кадра согласно варианту задания
FRAME_SIZE = 100

# Размеры служебных полей (могут быть разными, сейчас все по 1 байту)
FRAME_TYPE_SIZE = 1      # Размер поля "Тип кадра"
FRAME_NUMBER_SIZE = 1    # Размер поля "Номер кадра"
CHECKSUM_SIZE = 1        # Размер поля "Контрольная сумма"

RESERVED_SIZE = 10       # Размер зарезервированной области в конце кадра

# Фиксированный размер поля данных:
# 100 (весь кадр) - 8 (флаг) - 1 (тип) - 1 (номер) - 1 (контрольная сумма) - 10 (резерв) = 79 байт
DATA_FIELD_SIZE = 79

# Размер тела кадра в битах (без флага):
# (100 - 8) * 8 = 736 бит. Используется на стороне приёма.
BODY_BITS = (FRAME_SIZE - len(FRAME_FLAG)) * 8

# Названия полей кадра для отображения в окне статуса
FRAME_FIELD_NAMES = "Флаг Тип_кадра Номер_кадра Данные Контрольная_сумма"


# ============================================================
# СТРУКТУРА КАДРА
# ============================================================

# Класс Frame описывает структуру сетевого кадра фиксированной длины (100 байт).
# Содержит все шесть полей: флаг, три служебных поля, поле данных и резерв.
class Frame:

    # Конструктор класса: принимает полезные данные (до 79 байт)
    # и инициализирует все поля кадра.
    def __init__(self, data: bytes):
        # Проверка: размер входных данных не должен превышать
        # фиксированный размер поля данных (79 байт).
        if len(data) > DATA_FIELD_SIZE:
            raise ValueError("Поле данных превышает допустимый размер.")

        # Инициализация полей кадра.
        # Флаг и данные задаются явно, служебные поля и резерв — нулевыми.
        self.flag = FRAME_FLAG
        self.data = data

        # Служебные поля и резерв передаются нулевыми согласно заданию.
        # Ниже приведены примеры их возможного использования программой:

        # 1. self.frame_type (Тип кадра — показывает, что находится в кадре):
        #    Может использоваться для различия типов передаваемой информации.
        #    Например:
        #    0x00 — кадр с обычными данными (текст пользователя);
        #    0x01 — положительное подтверждение приёма (ACK);
        #    0x02 — отрицательное подтверждение (NAK), запрос повторной передачи;
        #    0x03 — служебная команда (например, установка или разрыв соединения).
        #    Пример: приёмник получил кадр с типом 0x00 — выводит данные в окно
        #    вывода и отправляет в ответ кадр с типом 0x01.
        self.frame_type = 0

        # 2. self.frame_number (Номер кадра — порядковый номер, 0..255):
        #    Может использоваться для контроля порядка и потери кадров.
        #    Например:
        #    передатчик нумерует кадры по кругу: 0, 1, 2, ..., 255, 0, ...;
        #    приёмник запоминает номер последнего принятого кадра. Если пришёл
        #    кадр с номером 5, а до этого был 3 — кадр 4 потерян;
        #    если пришёл уже принятый номер — это повтор, повторно в окно
        #    вывода он не выводится;
        #    в кадрах ACK/NAK в этом поле указывается номер подтверждаемого кадра.
        self.frame_number = 0

        # 3. self.checksum (Контрольная сумма — проверка целостности данных):
        #    Может использоваться для обнаружения искажений при передаче.
        #    Например:
        #    передатчик записывает сюда XOR всех байтов поля данных;
        #    приёмник считает XOR принятых данных заново и сравнивает с полем.
        #    Для данных "H" (код 72) в поле будет записано 72; если на линии
        #    байт исказился, значения не совпадут — кадр отбрасывается или
        #    отправляется NAK.
        self.checksum = 0

        # 4. self.reserved (10 зарезервированных байтов — для будущего использования):
        #    Может использоваться для расширения протокола.
        #    Например:
        #    версия протокола (1 байт);
        #    адрес отправителя и адрес получателя (по 1 байту);
        #    длина поля данных (1 байт) — чтобы не отбрасывать хвостовые нули;
        #    расширенная контрольная сумма CRC-16 (2 байта).
        self.reserved = bytes(RESERVED_SIZE)

    # Метод дополняет поле данных нулевыми байтами справа до фиксированного
    # размера (79 байт), обеспечивая постоянную длину кадра.
    def padded_data(self) -> bytes:
        # Дополнение поля данных нулями справа до фиксированного размера (79 байт).
        # Это гарантирует, что кадр всегда будет иметь строго заданную длину.
        return self.data.ljust(DATA_FIELD_SIZE, b'\x00')

    # Метод собирает все поля кадра в единый байтовый объект
    # строго заданной длины (100 байт) для последующей передачи.
    def to_bytes(self) -> bytes:
        # Сборка физического кадра ровно в 100 байт.
        # Поля собираются в байтовую строку в заданном порядке.
        body = (
            bytes([self.frame_type]) * FRAME_TYPE_SIZE        # Служебное поле 1: Тип кадра
            + bytes([self.frame_number]) * FRAME_NUMBER_SIZE  # Служебное поле 2: Номер кадра
            + self.padded_data()                              # Поле данных (79 байт)
            + bytes([self.checksum]) * CHECKSUM_SIZE          # Служебное поле 3: Контрольная сумма
            + self.reserved                                   # 10 зарезервированных байтов (в конце)
        )
        frame = self.flag + body

        # Финальная проверка длины собранного кадра
        if len(frame) != FRAME_SIZE:
            raise ValueError("Длина кадра должна быть ровно 100 байт.")
        return frame


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def bytes_to_bits(data: bytes) -> str:
    return ''.join(format(byte, '08b') for byte in data)


FLAG_BITS = bytes_to_bits(FRAME_FLAG)          # 64 бита флага
FLAG_LEN = len(FLAG_BITS)                       # = 64

# вставляемый бит — противоположный последнему биту флага.
STUFF_BIT = '1' if FLAG_BITS[-1] == '0' else '0'

# pi[i] = длина наибольшего собственного префикса FLAG_BITS[:i+1],
#         совпадающего с его суффиксом.
def build_prefix_function(pattern: str):
    pi = [0] * len(pattern)
    k = 0
    for i in range(1, len(pattern)):
        while k > 0 and pattern[i] != pattern[k]:
            k = pi[k - 1]
        if pattern[i] == pattern[k]:
            k += 1
        pi[i] = k
    return pi


FLAG_PI = build_prefix_function(FLAG_BITS)


def bits_to_bytes(bits: str) -> bytes:
    if len(bits) % 8 != 0:
        bits = bits.ljust(len(bits) + (8 - len(bits) % 8), '0')
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


def bit_stuff(bits: str) -> str:
    """
    Стаффинг по КМП: вставляем STUFF_BIT, когда в потоке накопились
    63 совпадающих бита флага (matched == FLAG_LEN - 1), то есть
    за один бит до появления полного флага.
    """
    result = []
    matched = 0
    for bit in bits:
        result.append(bit)

        # Обновление состояния КМП (как при поиске образца в тексте).
        while matched > 0 and bit != FLAG_BITS[matched]:
            matched = FLAG_PI[matched - 1]
        if bit == FLAG_BITS[matched]:
            matched += 1

        # Опасность: совпали 63 бита флага. Вставляем бит-отвлекатель.
        if matched == FLAG_LEN - 1:
            result.append(STUFF_BIT)
            # После вставки STUFF_BIT (он не равен FLAG_BITS[-1])
            # полный флаг сложиться не может. Пересчитываем состояние
            # КМП с учётом вставленного бита.
            stuffed = STUFF_BIT
            while matched > 0 and stuffed != FLAG_BITS[matched]:
                matched = FLAG_PI[matched - 1]
            if stuffed == FLAG_BITS[matched]:
                matched += 1
            # matched гарантированно < FLAG_LEN, потому что stuffed != FLAG_BITS[-1]

    return ''.join(result)


def format_field_bits(data: bytes) -> str:
    return ','.join(format(b, '08b') for b in data)


def format_stuffed_field(data: bytes, matched: int = 0):
    """
    Показывает вставленные биты подчёркнутыми и переносит состояние КМП
    между полями. Возвращает (HTML-строка, новое состояние matched).
    """
    byte_parts = []
    for byte in data:
        tokens = []
        for bit in format(byte, '08b'):
            tokens.append(bit)

            while matched > 0 and bit != FLAG_BITS[matched]:
                matched = FLAG_PI[matched - 1]
            if bit == FLAG_BITS[matched]:
                matched += 1

            if matched == FLAG_LEN - 1:
                tokens.append(f'<u>{STUFF_BIT}</u>')
                stuffed = STUFF_BIT
                while matched > 0 and stuffed != FLAG_BITS[matched]:
                    matched = FLAG_PI[matched - 1]
                if stuffed == FLAG_BITS[matched]:
                    matched += 1

        byte_parts.append(''.join(tokens))
    return ','.join(byte_parts), matched


# ============================================================
# ПОТОК ПРИЁМА
# ============================================================

class SerialReaderThread(QThread):

    data_received = pyqtSignal(bytes)
    error_occurred = pyqtSignal(str)

    def __init__(self, serial_port):
        super().__init__()
        self.serial_port = serial_port
        self.running = True

    def run(self):
        while self.running:
            if self.serial_port and self.serial_port.is_open:
                try:
                    if self.serial_port.in_waiting > 0:
                        data = self.serial_port.read(self.serial_port.in_waiting)
                        if data:
                            self.data_received.emit(data)
                    else:
                        self.msleep(40)
                except Exception as e:
                    self.error_occurred.emit(f"Ошибка чтения данных: {e}")
                    self.msleep(1000)

    def stop(self):
        self.running = False
        self.wait()


# ============================================================
# ПОЛЕ ВВОДА (как в main.py / лаб. №1)
# ============================================================

class CharTextEdit(QTextEdit):

    char_pressed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cursorPositionChanged.connect(self._lock_cursor_to_end)

    def _lock_cursor_to_end(self):
        cursor = self.textCursor()
        if not cursor.atEnd():
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cursor)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.moveCursor(QTextCursor.MoveOperation.End)

    def mouseDoubleClickEvent(self, event):
        self.moveCursor(QTextCursor.MoveOperation.End)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            event.accept()
            return

        forbidden_keys = (
            Qt.Key.Key_Left,
            Qt.Key.Key_Up,
            Qt.Key.Key_Home,
            Qt.Key.Key_PageUp
        )
        if event.key() in forbidden_keys:
            self.moveCursor(QTextCursor.MoveOperation.End)
            event.accept()
            return

        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return

        if event.text():
            self.char_pressed.emit(event.text())

        super().keyPressEvent(event)
        self.moveCursor(QTextCursor.MoveOperation.End)


# ============================================================
# ОСНОВНОЕ ОКНО
# ============================================================

class ComPortApp(QMainWindow):

    def __init__(self):
        super().__init__()

        self.serial = serial.Serial()
        self.reader_thread = None
        self.tx_count = 0
        self.tx_symbol_buffer = 0
        self.tx_buffer = bytearray()

        self.rx_buffer = bytearray()
        self.receiving_frame = False
        self.received_bits = []
        self.rx_matched = 0
        self.expect_stuff_bit = False

        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("COM-порт Мессенджер_1")
        self.resize(560, 480)
        self.setStyleSheet("QWidget { font-size: 14px; }")

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        control_group = QGroupBox("Окно управления")
        ctrl_layout = QHBoxLayout()

        self.port_combo = QComboBox()
        self.port_combo.setEditable(False)
        self.update_ports()
        self.port_combo.currentTextChanged.connect(self.try_lock_and_open_port)

        self.baudrate_combo = QComboBox()
        self.baudrate_combo.addItems([
            "110", "300", "600", "1200", "2400", "4800", "9600",
            "14400", "19200", "38400", "57600", "115200"
        ])
        self.baudrate_combo.setCurrentText("9600")
        self.baudrate_combo.currentTextChanged.connect(self.update_baudrate)

        ctrl_layout.addWidget(QLabel("COM-порт:"))
        ctrl_layout.addWidget(self.port_combo)
        ctrl_layout.addWidget(QLabel("Скорость:"))
        ctrl_layout.addWidget(self.baudrate_combo)
        control_group.setLayout(ctrl_layout)
        layout.addWidget(control_group)

        layout.addWidget(QLabel("Окно ввода:"))
        self.input_field = CharTextEdit()
        self.input_field.setMinimumHeight(100)
        self.input_field.char_pressed.connect(self.send_data)
        layout.addWidget(self.input_field)

        layout.addWidget(QLabel("Окно вывода:"))
        self.output_field = QTextEdit()
        self.output_field.setReadOnly(True)
        layout.addWidget(self.output_field)

        # Окно статуса как в main.py + дополнение для лаб.2 (поля кадра)
        status_group = QGroupBox("Окно статуса")
        status_layout = QVBoxLayout()

        self.status_label = QLabel("Передано символов: 0")
        self.status_label.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self.status_label)

        self.frame_state = QTextEdit()
        self.frame_state.setReadOnly(True)
        status_layout.addWidget(self.frame_state)

        status_group.setLayout(status_layout)
        layout.addWidget(status_group)

        # Заголовок окна статуса — названия полей кадра
        self.frame_state.setPlainText(FRAME_FIELD_NAMES)

    # ========================================================
    # COM-ПОРТЫ
    # ========================================================

    def update_ports(self):
        self.port_combo.clear()
        self.port_combo.addItem("")
        ports = [port.device for port in serial.tools.list_ports.comports()]
        if ports:
            self.port_combo.addItems(sorted(ports))
        self.port_combo.setCurrentIndex(0)

    def get_baudrate(self):
        try:
            return int(self.baudrate_combo.currentText())
        except ValueError:
            return 9600

    def try_lock_and_open_port(self, port_name):
        port_name = port_name.strip()
        if not port_name:
            return

        try:
            if self.serial.is_open:
                if self.reader_thread:
                    self.reader_thread.stop()
                    self.reader_thread = None
                self.serial.close()

            self.serial.port = port_name
            self.serial.baudrate = self.get_baudrate()
            self.serial.parity = serial.PARITY_NONE
            self.serial.stopbits = serial.STOPBITS_ONE
            self.serial.bytesize = serial.EIGHTBITS
            self.serial.timeout = 0.1
            self.serial.open()

            self.port_combo.setEnabled(False)

            self.tx_buffer.clear()
            self.tx_symbol_buffer = 0
            self.rx_buffer.clear()
            self.receiving_frame = False
            self.received_bits = []
            self.rx_matched = 0
            self.expect_stuff_bit = False

            self.reader_thread = SerialReaderThread(self.serial)
            self.reader_thread.data_received.connect(self.receive_data)
            self.reader_thread.error_occurred.connect(self.show_error)
            self.reader_thread.start()

        except Exception as e:
            self.show_error(f"Не удалось открыть порт: {e}")
            self.port_combo.setCurrentIndex(0)

    def update_baudrate(self):
        if not self.serial.is_open:
            return
        try:
            self.serial.baudrate = self.get_baudrate()
        except Exception as e:
            self.show_error(f"Не удалось изменить скорость: {e}")

    # ========================================================
    # ПЕРЕДАЧА: кадр отправляется после полного заполнения поля данных
    # ========================================================

    def send_data(self, char: str):
        if char in ('\n', '\r'):
            return

        if not self.serial.is_open:
            self.show_error("Сначала выберите и откройте COM-порт.")
            return

        try:
            data = char.encode('utf-8')

            if len(data) > DATA_FIELD_SIZE:
                self.show_error("Символ слишком большой для поля данных.")
                return

            if len(self.tx_buffer) + len(data) > DATA_FIELD_SIZE:
                if self.tx_buffer:
                    self.send_full_frame()

            self.tx_buffer.extend(data)
            self.tx_symbol_buffer += 1

            # Если буфер заполнился ровно до DATA_FIELD_SIZE — отправить
            if len(self.tx_buffer) == DATA_FIELD_SIZE:
                self.send_full_frame()

        except Exception as e:
            self.show_error(f"Ошибка передачи: {e}")

    # ========================================================
    # ОТПРАВКА ТЕКУЩЕГО БУФЕРА КАК КАДРА
    # ========================================================

    def send_full_frame(self):
        try:
            if not self.tx_buffer:
                return

            frame = Frame(bytes(self.tx_buffer))
            frame_bytes = frame.to_bytes()

            # Флаг без стаффинга; стаффится только тело кадра
            body = frame_bytes[len(FRAME_FLAG):]
            stuffed_body = bits_to_bytes(bit_stuff(bytes_to_bits(body)))
            self.serial.write(FRAME_FLAG + stuffed_body)

            self.tx_count += self.tx_symbol_buffer
            self.status_label.setText(f"Передано символов: {self.tx_count}")
            self.show_last_frame(frame)

            self.tx_buffer.clear()
            self.tx_symbol_buffer = 0

        except Exception as e:
            self.show_error(f"Ошибка передачи: {e}")

    # ========================================================
    # ОКНО СТАТУСА
    # ========================================================

    def show_last_frame(self, frame: Frame):
        """
        Названия полей, затем последний кадр до и после бит-стаффинга.
        Зарезервированные байты не выводятся. Формат — двоичный.
        Вставленные биты подчёркнуты.
        Порядок полей: Флаг, Тип_кадра, Номер_кадра, Данные, Контрольная_сумма.
        """
        flag_text = format_field_bits(frame.flag)

        type_before = format(frame.frame_type, '08b')
        number_before = format(frame.frame_number, '08b')
        checksum_before = format(frame.checksum, '08b')
        data_before = format_field_bits(frame.data) if frame.data else ''

        before = (
            f"{flag_text} {type_before} {number_before} {data_before} "
            f"{checksum_before}"
        )

        # Стаффинг выполняется непрерывно в порядке полей кадра.
        # Состояние КМП (matched) переносится между полями.
        matched = 0
        type_after, matched = format_stuffed_field(bytes([frame.frame_type]), matched)
        number_after, matched = format_stuffed_field(bytes([frame.frame_number]), matched)
        data_after, matched = format_stuffed_field(frame.data, matched)
        checksum_after, matched = format_stuffed_field(bytes([frame.checksum]), matched)

        after = (
            f"{flag_text} {type_after} {number_after} {data_after} "
            f"{checksum_after}"
        )

        # Заменяем обычные пробелы на неразрывные, чтобы браузер
        # не переносил строку после флага (и между полями вообще)
        before_html = before.replace(' ', '&nbsp;')
        after_html = after.replace(' ', '&nbsp;')

        html = (
            '<div style="white-space: pre-wrap;">'
            f'{FRAME_FIELD_NAMES}<br>'
            f'{before_html}<br>'
            f'{after_html}'
            '</div>'
        )
        self.frame_state.setHtml(html)

    # ========================================================
    # ПРИЁМ И ДЕСТАФФИНГ
    # ========================================================

    def _reset_rx_frame(self):
        self.receiving_frame = False
        self.received_bits = []
        self.rx_matched = 0
        self.expect_stuff_bit = False

    def receive_data(self, raw_data: bytes):
        self.rx_buffer.extend(raw_data)

        while True:
            if not self.receiving_frame:
                position = self.rx_buffer.find(FRAME_FLAG)
                if position == -1:
                    if len(self.rx_buffer) > len(FRAME_FLAG):
                        self.rx_buffer = self.rx_buffer[-(len(FRAME_FLAG) - 1):]
                    return

                del self.rx_buffer[:position + len(FRAME_FLAG)]
                self.receiving_frame = True
                self.received_bits = []
                self.rx_matched = 0
                self.expect_stuff_bit = False

            if not self.rx_buffer:
                return

            current_byte = self.rx_buffer.pop(0)
            frame_finished = False
            frame_error = False

            for bit in format(current_byte, '08b'):
                # Ожидаем вставленный бит-отвлекатель.
                if self.expect_stuff_bit:
                    if bit != STUFF_BIT:
                        frame_error = True
                        break
                    self.expect_stuff_bit = False
                    # Пересчитываем состояние КМП с учётом вставленного бита,
                    # но НЕ добавляем его в received_bits (он служебный).
                    while self.rx_matched > 0 and bit != FLAG_BITS[self.rx_matched]:
                        self.rx_matched = FLAG_PI[self.rx_matched - 1]
                    if bit == FLAG_BITS[self.rx_matched]:
                        self.rx_matched += 1
                    if len(self.received_bits) >= BODY_BITS:
                        frame_finished = True
                        break
                    continue

                self.received_bits.append(bit)

                # Обновление состояния КМП по принятому биту.
                while self.rx_matched > 0 and bit != FLAG_BITS[self.rx_matched]:
                    self.rx_matched = FLAG_PI[self.rx_matched - 1]
                if bit == FLAG_BITS[self.rx_matched]:
                    self.rx_matched += 1

                # Опасность: 63 совпавших бита → ждём вставленный бит.
                if self.rx_matched == FLAG_LEN - 1:
                    self.expect_stuff_bit = True

                if len(self.received_bits) >= BODY_BITS and not self.expect_stuff_bit:
                    frame_finished = True
                    break

            if frame_error:
                self._reset_rx_frame()
                continue

            if not frame_finished:
                continue

            body = bits_to_bytes(''.join(self.received_bits[:BODY_BITS]))
            # Служебные поля идут сразу после флага, перед данными:
            # Тип (1) + Номер (1) = 2 байта.
            # Далее — 79 байт данных, затем Контрольная сумма (1), затем 10 зарезервированных (их не читаем).
            data_start = FRAME_TYPE_SIZE + FRAME_NUMBER_SIZE
            data = body[data_start:data_start + DATA_FIELD_SIZE].rstrip(b'\x00')

            if data:
                text = data.decode('utf-8', errors='replace')
                self.output_field.insertPlainText(text)
                self.output_field.ensureCursorVisible()

            self._reset_rx_frame()

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)

    def closeEvent(self, event):
        if self.reader_thread:
            self.reader_thread.stop()
            self.reader_thread = None
        if self.serial.is_open:
            self.serial.close()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ComPortApp()
    window.show()
    sys.exit(app.exec())