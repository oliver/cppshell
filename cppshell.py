#!/usr/bin/python

#
# Interactive C++ shell - you type C++ code, and it's compiled and executed on the fly in the background.
#


import sys
import os
import re
import tempfile
import subprocess
import fcntl
import gobject
import gtk
import gtk.glade
import pango


class GProcess:
    def __init__ (self, cmd, onFinished=None, onStdout=None, onStderr=None, env=None):
        self.onFinished = onFinished

        self.fdIn = open('/dev/null', 'r')
        self.proc = subprocess.Popen(cmd, stdin=self.fdIn.fileno(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

        # make pipes non-blocking:
        fl = fcntl.fcntl(self.proc.stdout, fcntl.F_GETFL)
        fcntl.fcntl(self.proc.stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        fl = fcntl.fcntl(self.proc.stderr, fcntl.F_GETFL)
        fcntl.fcntl(self.proc.stderr, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        gobject.io_add_watch(self.proc.stdout, gobject.IO_IN | gobject.IO_ERR | gobject.IO_HUP,
            self._onReadable, onStdout)
        gobject.io_add_watch(self.proc.stderr, gobject.IO_IN | gobject.IO_ERR | gobject.IO_HUP,
            self._onReadable, onStderr)
        self.pipesOpen = 2

    def _onReadable (self, fd, cond, callback):
        if (cond & gobject.IO_IN):
            readText = fd.read(4000)
            if callback:
                callback(readText)
            return True
        else:
            # read all remaining data from pipe
            while True:
                readText = fd.read(4000)
                if len(readText) <= 0:
                    break
                if callback:
                    callback(readText)

            fd.close()
            self.pipesOpen -= 1
            if self.pipesOpen <= 0:
                exitCode = self.proc.wait()
                print "exitCode: %d" % exitCode
                assert(exitCode is not None) # child should have terminated now

                if self.onFinished:
                    self.onFinished(exitCode)
                self.fdIn.close()
            return False


cppTemplate = """

#line 1 "_user_code_include_"
%s
#line 1 "_generated_code_main_start_"

#include <iostream>
using namespace std;

int main (int argc, char* argv[])
{

#line 1 "_user_code_main_"
%s

}
"""


class Compiler:
    def __init__ (self, userCode, onFinishedCb):
        self.onFinishedCb = onFinishedCb
        self.output = ""

        (cppText, self.numIncludeLines) = self.translateCode(userCode)

        (fileno, self.tempFile) = tempfile.mkstemp(prefix='cpp-', suffix='.C', text=True)
        fd = os.fdopen(fileno, 'w')
        fd.write(cppText)
        fd.close()

        self.exePath = tempfile.mktemp(prefix='cpp-', suffix='.bin')

        cmd = ['g++', '-W', '-Wall', '-Wextra', self.tempFile, '-o', self.exePath]
        print cmd

        env = os.environ
        env['LANG'] = ''
        self.proc = GProcess(cmd, self.onProcFinished, self.onOutput, self.onOutput, env)

    def onProcFinished (self, exitCode):
        (errors, warnings) = self.parseOutput(self.output)
        if exitCode == 0:
            os.unlink(self.tempFile)
            self.onFinishedCb(self.exePath, errors, warnings)
        else:
            os.rename(self.tempFile, '/tmp/cppshell-failed-code.C')
            if not(errors):
                errors = [ ('(unknown error)', 1) ]
            self.onFinishedCb(None, errors, warnings)

    def onOutput (self, text):
        self.output += text

    def translateCode (self, userCode):
        includeLines = ""
        codeLines = ""
        numIncludeLines = 0
        lineNo = 0
        for line in userCode.splitlines(True):
            lineNo+=1
            if re.search(r'^\s*#include', line):
                includeLines += '#line %d "_user_code_include_"\n' % (lineNo)
                includeLines += line
                numIncludeLines += 1
            else:
                codeLines += line

        cppText = cppTemplate % (includeLines, codeLines)
        return (cppText, numIncludeLines)

    def parseOutput (self, output):
        errors = []
        warnings = []
        for l in output.splitlines():
            #print "C: " + l
            try:
                (loc, msg) = l.split(': ', 1)
            except:
                # ignore
                continue

            locTuple = loc.split(':')
            if len(locTuple) < 2:
                # ignore messages without line number
                continue

            lineNo = int(locTuple[1])

            if locTuple[0] == '_user_code_include_':
                pass
            elif locTuple[0] == '_user_code_main_':
                lineNo += self.numIncludeLines
            else:
                # ignore messages for code not entered by user
                continue

            if msg.startswith('error: '):
                innerMsg = msg[7:]
                errors.append( (innerMsg, lineNo) )
            elif msg.startswith('warning: '):
                innerMsg = msg[9:]
                warnings.append( (innerMsg, lineNo) )

        return (errors, warnings)


class Executer:
    def __init__ (self, command, finishedCb, outputCb):
        self.finishedCb = finishedCb
        self.outputCb = outputCb

        cmd = [command]
        print cmd

        self.proc = GProcess(cmd, self.onProcFinished, self.onStdout, self.onStderr)

    def onProcFinished (self, exitCode):
        self.finishedCb(exitCode)

    def onStdout (self, text):
        self.outputCb(text, 'stdout')

    def onStderr (self, text):
        self.outputCb(text, 'stderr')


(STATE_INITIAL, STATE_COMPILING, STATE_RUNNING, STATE_FINISHED) = range(0, 4)


class Task:
    def __init__ (self, inText, onOutput, onStateChanged):
        self.inputText = inText
        self.onOutput = onOutput
        self.onStateChanged = onStateChanged
        self.errorDetails = None
        self._compilerResult = None
        self._runExitCode = None
        self.state = STATE_INITIAL
        self.exePath = None
        self.outputText = None

    def setState (self, newState):
        oldState = self.state
        self.state = newState
        self.onStateChanged(self, newState, oldState)

    def error (self):
        return self.errorDetails

    def compilerResult (self):
        return self._compilerResult

    def exitCode (self):
        return self._runExitCode

    def start (self, taskFinishedCb):
        self.taskFinishedCb = taskFinishedCb
        self.work()

    def work (self):
        if self.exePath is None:
            self.setState(STATE_COMPILING)
            self.compiler = Compiler(self.inputText, self._onCompileFinished)
        elif self.outputText is None:
            self.setState(STATE_RUNNING)
            self.executer = Executer(self.exePath, self._onExecFinished, self.onOutput)
        else:
            # should not happen
            assert(False)

    def _onCompileFinished (self, exePath, errors, warnings):
        print "compile finished; exe: '%s'" % exePath
        self._compilerResult = (errors, warnings)
        if errors:
            self.errorDetails = str(errors)
            self.setState(STATE_FINISHED)
            self.taskFinishedCb()
        else:
            assert(exePath is not None)
            self.exePath = exePath
            self.compiler = None
            self.work()

    def _onExecFinished (self, exitCode):
        print "execution finished"
        self._runExitCode = exitCode
        self.outputText = "abc"
        self.setState(STATE_FINISHED)
        self.taskFinishedCb()

#    def _onOutput (self, line):
#        print "O: " + line


class ExecQueue:
    "holds the next state that has to be compiled and executed"
    def __init__ (self):
        self.tActive = None
        self.tQueued = None

    def enqueue (self, task):
        if self.tActive is None:
            self.tActive = task
            self.startWorking()
        else:
            # replace existing queued task
            self.tQueued = task

    def startWorking (self):
        # start working on self.tActive
        self.tActive.start(self._onTaskFinished)

    def _onTaskFinished (self):
        print "task finished"
        self.tActive = None
        if self.tQueued is not None:
            self.tActive = self.tQueued
            self.tQueued = None
            self.startWorking()



MARGIN_WIDTH = 24

class CppShellGui:
    def __init__ (self):
        gladeFile = os.path.join(os.path.realpath( os.path.dirname(sys.argv[0]) ), 'cppshell.glade')
        self.tree = gtk.glade.XML(gladeFile, 'winMain')
        self.tree.signal_autoconnect(self)

        self.txtIn = self.tree.get_widget('txtInput')
        self.bufferIn = gtk.TextBuffer()
        self.txtIn.set_buffer(self.bufferIn)

        self.numInputLines = 0
        self.bufferIn.connect('changed', self.onInputChanged)

        self.txtIn.set_border_window_size(gtk.TEXT_WINDOW_LEFT, MARGIN_WIDTH)

        # maps from line number (1-based) to marker widget
        self.markers = {}

        self.txtOut = self.tree.get_widget('txtOutput')
        self.bufferOut = gtk.TextBuffer()
        self.txtOut.set_buffer(self.bufferOut)
        self.tagStderr = self.bufferOut.create_tag(foreground='red')

        pangoFont = pango.FontDescription("Monospace")
        self.txtIn.modify_font(pangoFont)
        self.txtOut.modify_font(pangoFont)

        accel_group = gtk.AccelGroup()
        self.tree.get_widget("winMain").add_accel_group(accel_group)
        self.tree.get_widget("tbExecute").add_accelerator("clicked", accel_group,
            gtk.keysyms.Return, gtk.gdk.CONTROL_MASK, gtk.ACCEL_VISIBLE)
        self.tree.get_widget("tbExecute").add_accelerator("clicked", accel_group,
            gtk.keysyms.KP_Enter, gtk.gdk.CONTROL_MASK, gtk.ACCEL_VISIBLE)

        if not(hasattr(self.txtIn, 'set_tooltip_text')):
            self.tooltipsObject = gtk.Tooltips()
        else:
            self.tooltipsObject = None

        self.queue = ExecQueue()

        self.saveFileName = os.path.expanduser('~/.config/cppshell.txt')
        try:
            fd = open(self.saveFileName, 'r')
            text = fd.read()
            fd.close()
            self.bufferIn.set_text(text)
        except:
            pass

    def on_txtInput_expose_event (self, widget, event):
        if self.txtIn.get_window_type(event.window) == gtk.TEXT_WINDOW_LEFT:
            for lineNo, widget in self.markers.items():
                it = self.bufferIn.get_iter_at_line(lineNo-1)
                (bufferY, bufferHeight) = self.txtIn.get_line_yrange(it)
                (x, y) = self.txtIn.buffer_to_window_coords(gtk.TEXT_WINDOW_LEFT, -MARGIN_WIDTH, bufferY)
                self.txtIn.move_child(widget, x, y)

        return False

    def on_winMain_delete_event (self, widget, dummy):
        gtk.main_quit()

    def onInputChanged (self, buffer):
        numLines = buffer.get_line_count()
        if numLines != self.numInputLines:
            self.numInputLines = numLines
            self.execute()

    def on_tbExecute_clicked (self, button):
        self.execute()

    def execute (self):
        text = self.bufferIn.get_text(self.bufferIn.get_start_iter(), self.bufferIn.get_end_iter())

        task = Task(text, self.onOutput, self.onTaskChanged)
        self.queue.enqueue(task)

        fd = open(self.saveFileName, 'w')
        fd.write(text)
        fd.close()

    def onTaskChanged (self, task, newState, oldState):
        "called when current Task makes a state change"
        imgStatus = self.tree.get_widget('imgStatus')
        iconSize = gtk.ICON_SIZE_MENU
        if newState == STATE_COMPILING:
            imgStatus.set_from_stock(gtk.STOCK_CONVERT, iconSize)
        elif newState == STATE_RUNNING:
            imgStatus.set_from_stock(gtk.STOCK_EXECUTE, iconSize)
            self.bufferOut.set_text('')
        elif newState == STATE_FINISHED:
            if task.error() is None:
                imgStatus.set_from_stock(gtk.STOCK_YES, iconSize)
            else:
                imgStatus.set_from_stock(gtk.STOCK_NO, iconSize)
        else:
            imgStatus.clear()

        self.tree.get_widget('lblStatus').set_text('')
        if oldState == STATE_COMPILING:
            compilerResult = task.compilerResult()
            assert(compilerResult is not None)
            if compilerResult[0]:
                self.tree.get_widget('lblStatus').set_text('compilation failed')

            self.clearMarkers()
            for (w,l) in compilerResult[1]:
                self.setMarker(l, w, 'warning')
            for (w,l) in compilerResult[0]:
                self.setMarker(l, w, 'error')
        elif oldState == STATE_RUNNING:
            exitCode = task.exitCode()
            assert(exitCode is not None)
            if exitCode > 0:
                self.tree.get_widget('lblStatus').set_text('exit code: %d' % exitCode)
            elif exitCode < 0:
                self.tree.get_widget('lblStatus').set_text('killed by signal %d' % (-exitCode))

    def onOutput (self, text, typ):
        if typ == 'stderr':
            startOffset = self.bufferOut.get_end_iter().get_offset()
            self.bufferOut.insert(self.bufferOut.get_end_iter(), text)
            start = self.bufferOut.get_iter_at_offset(startOffset)
            end = self.bufferOut.get_end_iter()
            self.bufferOut.apply_tag(self.tagStderr, start, end)
        else:
            self.bufferOut.insert(self.bufferOut.get_end_iter(), text)

    def clearMarkers (self):
        for lineNo, widget in self.markers.items():
            self.txtIn.remove(widget)
        self.markers = {}

    def setMarker (self, lineNo, text, typ):
        if self.markers.has_key(lineNo):
            widget = self.markers[lineNo]
            self.txtIn.remove(widget)

        widget = gtk.Image()
        if typ == 'warning':
            widget.set_from_stock(gtk.STOCK_DIALOG_WARNING, gtk.ICON_SIZE_MENU)
        else:
            widget.set_from_stock(gtk.STOCK_DIALOG_ERROR, gtk.ICON_SIZE_MENU)

        if not(self.tooltipsObject):
            widget.set_tooltip_text(text)
        else:
            # use old tooltips API for GTK < 2.12:
            eventBox = gtk.EventBox()
            eventBox.add(widget)
            self.tooltipsObject.set_tip(eventBox, text)
            widget = eventBox

        widget.show_all()
        self.txtIn.add_child_in_window(widget, gtk.TEXT_WINDOW_LEFT, 0, 0)
        self.markers[lineNo] = widget

        self.txtIn.queue_draw()

if __name__ == '__main__':
    gui = CppShellGui()
    gtk.main()

