import sys
import serial
import serial.tools.list_ports

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QComboBox, QTextEdit,
    QLineEdit, QMessageBox, QGroupBox
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt


# ============================================================
# ПАРАМЕТРЫ КАДРА (Вариант 1, Группа 450502)
# ============================================================

# 45050 + последняя цифра группы (2) + "-" + вариант (1)
FRAME_FLAG = b"450502-1"

FLAG_SIZE = 8

# Вариант 1: максимальная длина кадра — 100 байт
MAX_FRAME_SIZE = 100

# Три служебных поля по 1 байту
SERVICE_FIELD_SIZE = 1

# 10 зарезервированных байтов в конце кадра
RESERVED_SIZE = 10

# При передаче максимальный размер поля данных:
# 100 - 8 - 1 - 1 - 1 - 10 = 79 байт
MAX_DATA_SIZE = (
    MAX_FRAME_SIZE
    - FLAG_SIZE
    - 3 * SERVICE_FIELD_SIZE
    - RESERVED_SIZE
)


# ============================================================
# СТРУКТУРА КАДРА
# ============================================================

class Frame:
    """
    Структура кадра (всего 100 байт до стаффинга):

    1. Флаг начала кадра (8 байт: "450502-1")
    2. Поле данных (79 байт)
    3. Тип кадра (1 байт)
    4. Номер кадра (1 байт)
    5. Контрольная сумма (1 байт)
    6. 10 зарезервированных байтов

    Назначение служебных полей в данной программе:
    - frame_type: идентификатор типа передаваемых данных
      (0x00 - обычный пользовательский символ, 0x01 - управляющая команда);
    - frame_number: порядковый номер кадра (0..255) для контроля
      потери или нарушения порядка приёма символов;
    - checksum: простая контрольная сумма (XOR всех байтов данных)
      для проверки целостности содержимого кадра при приёме.
    """

    def __init__(self, data):
        self.flag = FRAME_FLAG
        self.data = data

        # Служебные поля пока не используются (передаются нулевыми)
        self.frame_type = 0
        self.frame_number = 0
        self.checksum = 0

        # Зарезервированные байты
        self.reserved = bytes(RESERVED_SIZE)

    def to_bytes(self):
        """
        Формирует физический кадр объёмом ровно 100 байт.
        """
        if len(self.data) > MAX_DATA_SIZE:
            raise ValueError("Поле данных превышает допустимый размер.")

        # Поле данных дополняется нулями до 79 байт
        padded_data = self.data.ljust(MAX_DATA_SIZE, b'\x00')

        body = (
            padded_data
            + bytes([self.frame_type])
            + bytes([self.frame_number])
            + bytes([self.checksum])
            + self.reserved
        )

        return self.flag + body


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def bytes_to_bits(data):
    """Преобразование байтов в последовательность битов."""
    return ''.join(format(byte, '08b') for byte in data)


def bits_to_bytes(bits):
    """Преобразование битов в байты с дополнением нулями."""
    result = bytearray()
    for i in range(0, len(bits), 8):
        byte_bits = bits[i:i + 8]
        if len(byte_bits) < 8:
            byte_bits = byte_bits.ljust(8, '0')
        result.append(int(byte_bits, 2))
    return bytes(result)


def bit_stuff(bits):
    """
    Классический бит-стаффинг:
    После каждых 5 единиц подряд вставляется бит '0'.
    """
    result = []
    ones_count = 0

    for bit in bits:
        result.append(bit)
        if bit == '1':
            ones_count += 1
            if ones_count == 5:
                result.append('0')
                ones_count = 0
        else:
            ones_count = 0

    return ''.join(result)


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
# ПОЛЕ ПОСИМВОЛЬНОГО ВВОДА
# ============================================================

class CharLineEdit(QLineEdit):

    char_pressed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cursorPositionChanged.connect(self.lock_cursor)

    def lock_cursor(self):
        end_position = len(self.text())
        if self.cursorPosition() != end_position:
            self.setCursorPosition(end_position)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.end(False)

    def mouseDoubleClickEvent(self, event):
        self.end(False)

    def keyPressEvent(self, event):
        forbidden_keys = (
            Qt.Key.Key_Left,
            Qt.Key.Key_Up,
            Qt.Key.Key_Home,
            Qt.Key.Key_PageUp
        )

        if event.key() in forbidden_keys:
            self.end(False)
            event.accept()
            return

        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.char_pressed.emit('\n')
        elif event.text():
            self.char_pressed.emit(event.text())

        super().keyPressEvent(event)
        self.end(False)


# ============================================================
# ОСНОВНОЕ ОКНО
# ============================================================

class ComPortApp(QMainWindow):

    def __init__(self):
        super().__init__()

        self.serial = serial.Serial()
        self.reader_thread = None
        self.tx_count = 0

        # Буфер приёма
        self.rx_buffer = bytearray()
        self.receiving_frame = False
        self.received_bits = []
        self.ones_count = 0

        self.init_ui()

    # ========================================================
    # ИНТЕРФЕЙС
    # ========================================================

    def init_ui(self):
        self.setWindowTitle("COM-порт Мессенджер_2")
        self.resize(560, 480)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # ----------------------------------------------------
        # УПРАВЛЕНИЕ (2 элемента выбора по ТЗ)
        # ----------------------------------------------------
        control_group = QGroupBox("Окно управления")
        control_layout = QHBoxLayout()

        control_layout.addWidget(QLabel("COM-порт:"))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(False)
        self.update_ports()
        self.port_combo.currentTextChanged.connect(self.try_lock_and_open_port)
        control_layout.addWidget(self.port_combo)

        control_layout.addWidget(QLabel("Скорость:"))
        self.baudrate_combo = QComboBox()
        self.baudrate_combo.addItems([
            "110", "300", "600", "1200", "2400", "4800",
            "9600", "14400", "19200", "38400", "57600", "115200"
        ])
        self.baudrate_combo.setCurrentText("9600")
        self.baudrate_combo.currentTextChanged.connect(self.update_baudrate)
        control_layout.addWidget(self.baudrate_combo)

        control_group.setLayout(control_layout)
        layout.addWidget(control_group)

        # ----------------------------------------------------
        # ВВОД
        # ----------------------------------------------------
        layout.addWidget(QLabel("Строка ввода (посимвольная передача):"))
        self.input_field = CharLineEdit()
        self.input_field.char_pressed.connect(self.send_data)
        layout.addWidget(self.input_field)

        # ----------------------------------------------------
        # ВЫВОД
        # ----------------------------------------------------
        layout.addWidget(QLabel("Окно вывода (принятые сообщения):"))
        self.output_field = QTextEdit()
        self.output_field.setReadOnly(True)
        layout.addWidget(self.output_field)

        # ----------------------------------------------------
        # СОСТОЯНИЕ
        # ----------------------------------------------------
        status_group = QGroupBox("Окно статуса")
        status_layout = QVBoxLayout()

        self.status_label = QLabel("Передано кадров: 0")
        self.status_label.setStyleSheet("font-weight: bold;")
        status_layout.addWidget(self.status_label)

        self.frame_state = QTextEdit()
        self.frame_state.setReadOnly(True)
        status_layout.addWidget(self.frame_state)

        status_group.setLayout(status_layout)
        layout.addWidget(status_group)

        # Единый заголовок из 5 отображаемых полей (без зарезервированных байтов)
        self.frame_state.setPlainText("Флаг Данные Тип_кадра Номер_кадра Контрольная_сумма")

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
    # ПЕРЕДАЧА ДАННЫХ
    # ========================================================

    def send_data(self, char):
        if not self.serial.is_open:
            self.show_error("Сначала выберите и откройте COM-порт.")
            return

        try:
            data = char.encode('utf-8')
            if len(data) > MAX_DATA_SIZE:
                self.show_error("Символ слишком большой для поля данных.")
                return

            frame = Frame(data)
            frame_bytes = frame.to_bytes()

            # Тело кадра без флага
            body = frame_bytes[FLAG_SIZE:]
            body_bits = bytes_to_bits(body)
            stuffed_bits = bit_stuff(body_bits)
            stuffed_body = bits_to_bytes(stuffed_bits)

            transmitted_frame = FRAME_FLAG + stuffed_body
            self.serial.write(transmitted_frame)

            self.tx_count += 1
            self.status_label.setText(f"Передано кадров: {self.tx_count}")

            self.show_last_frame(frame)

        except Exception as e:
            self.show_error(f"Ошибка передачи: {e}")

    # ========================================================
    # ОТОБРАЖЕНИЕ В ОКНЕ СТАТУСА
    # ========================================================

    def show_last_frame(self, frame):
        """
        Выводит названия полей и состояние последнего кадра
        до и после бит-стаффинга в стандартном системном шрифте.
        """
        # --- ДО СТАФФИНГА ---
        flag_text = ','.join(format(b, '08b') for b in frame.flag)
        data_before = ','.join(format(b, '08b') for b in frame.data)
        type_before = format(frame.frame_type, '08b')
        number_before = format(frame.frame_number, '08b')
        checksum_before = format(frame.checksum, '08b')

        before = f"{flag_text} {data_before} {type_before} {number_before} {checksum_before}"

        # --- ПОСЛЕ СТАФФИНГА ---
        data_after = self.stuffed_data_for_display(frame.data)
        type_after = "00000000"
        number_after = "00000000"
        checksum_after = "00000000"

        after = f"{flag_text} {data_after} {type_after} {number_after} {checksum_after}"

        # Используем обычную верстку без специфичных моноширинных семейств шрифта
        html = f"""
        <div style="white-space: pre-wrap;">
Флаг Данные Тип_кадра Номер_кадра Контрольная_сумма<br>
{before}<br>
{after}
        </div>
        """
        self.frame_state.setHtml(html)

    def stuffed_data_for_display(self, data):
        """
        Формирует двоичное представление поля данных после бит-стаффинга.
        Вставленный бит '0' подчёркивается (<u>0</u>).
        """
        result = []
        ones_count = 0

        for byte in data:
            value = []
            bits = format(byte, '08b')
            for bit in bits:
                value.append(bit)
                if bit == '1':
                    ones_count += 1
                    if ones_count == 5:
                        value.append("<u>0</u>")
                        ones_count = 0
                else:
                    ones_count = 0
            result.append(''.join(value))

        return ','.join(result)

    # ========================================================
    # ПРИЁМ ДАННЫХ И ДЕСТАФФИНГ
    # ========================================================

    def receive_data(self, raw_data):
        self.rx_buffer.extend(raw_data)

        while True:
            # 1. Поиск начала кадра
            if not self.receiving_frame:
                position = self.rx_buffer.find(FRAME_FLAG)
                if position == -1:
                    if len(self.rx_buffer) > FLAG_SIZE:
                        self.rx_buffer = self.rx_buffer[-(FLAG_SIZE - 1):]
                    return

                del self.rx_buffer[:position + FLAG_SIZE]
                self.receiving_frame = True
                self.received_bits = []
                self.ones_count = 0

            # 2. Приём и дестаффинг битов тела кадра
            if not self.rx_buffer:
                return

            current_byte = self.rx_buffer.pop(0)
            bits = format(current_byte, '08b')
            frame_finished = False

            for bit in bits:
                if self.ones_count == 5:
                    if bit == '0':
                        # Удаление вставленного бита стаффинга
                        self.ones_count = 0
                        continue
                    else:
                        # Ошибка кадра
                        self.receiving_frame = False
                        self.received_bits = []
                        self.ones_count = 0
                        break

                self.received_bits.append(bit)
                if bit == '1':
                    self.ones_count += 1
                else:
                    self.ones_count = 0

                # Принято полное исходное тело кадра (92 байта * 8 = 736 бит)
                if len(self.received_bits) >= (MAX_DATA_SIZE + 3 + RESERVED_SIZE) * 8:
                    frame_finished = True
                    break

            if not frame_finished:
                continue

            # 3. Извлечение данных из принятого кадра
            body_bits = ''.join(self.received_bits[:(MAX_DATA_SIZE + 3 + RESERVED_SIZE) * 8])
            body = bits_to_bytes(body_bits)

            # Выделение поля данных и удаление автодополнения нулями
            data = body[:MAX_DATA_SIZE].rstrip(b'\x00')

            if data:
                try:
                    text = data.decode('utf-8', errors='replace')
                    self.output_field.insertPlainText(text)
                    self.output_field.ensureCursorVisible()
                except Exception as e:
                    self.show_error(f"Ошибка обработки данных: {e}")

            self.receiving_frame = False
            self.received_bits = []
            self.ones_count = 0

    # ========================================================
    # ОШИБКИ И ЗАКРЫТИЕ
    # ========================================================

    def show_error(self, message):
        QMessageBox.critical(self, "Ошибка", message)

    def closeEvent(self, event):
        if self.reader_thread:
            self.reader_thread.stop()
            self.reader_thread = None
        if self.serial.is_open:
            self.serial.close()
        event.accept()


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ComPortApp()
    window.show()
    sys.exit(app.exec())