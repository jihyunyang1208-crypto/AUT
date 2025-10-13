import sys
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QPushButton, QDateEdit
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtCore import QDate, Qt
from trading_report.report_api import get_report_html

class ReportDialog(QDialog):
    def __init__(self, date_str: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{date_str} 데일리 리포트")
        self.setGeometry(150, 150, 1000, 800)
        self.setMinimumSize(800, 600)

        # Layout
        layout = QVBoxLayout(self)
        self.web_view = QWebEngineView()
        layout.addWidget(self.web_view)

        # Load report content
        self.load_report(date_str)

    def load_report(self, date_str: str):
        """지정된 날짜의 리포트를 분석하고 HTML로 변환하여 표시합니다."""
        self.setWindowTitle(f"{date_str} 데일리 리포트")
        # report_api를 통해 최종 HTML을 가져옵니다.
        html_content = get_report_html(date_str)
        self.web_view.setHtml(html_content)

# 이 파일을 직접 실행하여 테스트할 수 있는 코드
if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    # 테스트를 위한 메인 위젯
    class TestWidget(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("리포트 뷰어 테스트")
            layout = QVBoxLayout(self)
            self.date_edit = QDateEdit(QDate.currentDate())
            self.date_edit.setCalendarPopup(True)
            self.btn = QPushButton("선택한 날짜의 리포트 보기")
            self.btn.clicked.connect(self.show_report)
            layout.addWidget(self.date_edit)
            layout.addWidget(self.btn)
            self.resize(300, 100)
        
        def show_report(self):
            date_str = self.date_edit.date().toString("yyyy-MM-dd")
            # ReportDialog를 생성하고 모달(modal) 형태로 실행합니다.
            dialog = ReportDialog(date_str, self)
            dialog.exec()

    test_widget = TestWidget()
    test_widget.show()
    sys.exit(app.exec())
