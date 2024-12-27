#include <sys/resource.h>

#include <QApplication>
#include <QTranslator>

#include "system/hardware/hw.h"
#include "selfdrive/ui/qt/qt_window.h"
#include "selfdrive/ui/qt/util.h"
#include "selfdrive/ui/qt/window.h"
void customMessageHandler(QtMsgType type, const QMessageLogContext& context, const QString& msg) {
    QByteArray localMsg = msg.toLocal8Bit();
    const char* file = context.file ? context.file : "";
    const char* function = context.function ? context.function : "";
    int line = context.line;

    // 메시지 타입에 따라 출력 형식 설정
    switch (type) {
    case QtDebugMsg:
        fprintf(stderr, "Debug: %s (%s:%d, %s)\n", localMsg.constData(), file, line, function);
        break;
    case QtInfoMsg:
        fprintf(stderr, "Info: %s (%s:%d, %s)\n", localMsg.constData(), file, line, function);
        break;
    case QtWarningMsg:
        fprintf(stderr, "Warning: %s (%s:%d, %s)\n", localMsg.constData(), file, line, function);
        break;
    case QtCriticalMsg:
        fprintf(stderr, "Critical: %s (%s:%d, %s)\n", localMsg.constData(), file, line, function);
        break;
    case QtFatalMsg:
        fprintf(stderr, "Fatal: %s (%s:%d, %s)\n", localMsg.constData(), file, line, function);
        abort();
    }
}
int main(int argc, char *argv[]) {
  setpriority(PRIO_PROCESS, 0, -20);

  qInstallMessageHandler(customMessageHandler);
  //qInstallMessageHandler(swagLogMessageHandler);
  initApp(argc, argv);

  QTranslator translator;
  QString translation_file = QString::fromStdString(Params().get("LanguageSetting"));
  if (!translator.load(QString(":/%1").arg(translation_file)) && translation_file.length()) {
    qCritical() << "Failed to load translation file:" << translation_file;
  }

  QApplication a(argc, argv);
  a.installTranslator(&translator);

  MainWindow w;
  setMainWindow(&w);
  a.installEventFilter(&w);
  return a.exec();
}
