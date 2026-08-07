"""深色渐变背景控件（毛玻璃主题的底层氛围，启动时一次性绘制）。"""
from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QPainter, QLinearGradient, QColor, QPaintEvent


class GradientBackground(QWidget):
    """绘制深色渐变背景，为毛玻璃效果提供底层氛围"""
    def paintEvent(self, event: QPaintEvent):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0.0, QColor(18, 18, 36))
        grad.setColorAt(0.4, QColor(22, 22, 42))
        grad.setColorAt(0.7, QColor(28, 24, 48))
        grad.setColorAt(1.0, QColor(20, 20, 38))
        painter.fillRect(self.rect(), grad)
        painter.end()
