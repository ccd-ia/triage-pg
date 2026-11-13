;;; triage.el --- A magit-style front end for triage-pg projects  -*- lexical-binding: t; -*-

;; Copyright (c) 2026 Adolfo De Unánue and triage-pg contributors

;; Author: Adolfo De Unánue
;; URL: https://github.com/ccd-ia/triage-pg
;; Version: 1.6.0
;; Package-Requires: ((emacs "29.1") (magit-section "4.0") (transient "0.7"))
;; Keywords: tools, data
;; SPDX-License-Identifier: MIT

;; This file is not part of GNU Emacs.

;;; Commentary:

;; triage.el shows a triage-pg Project from Emacs.  `triage-status'
;; opens a buffer with the Project's Experiments (each matched to its
;; config file), recent Runs, declared Sources and the tables of the
;; triage schema.  RET opens an Experiment, a Run, a model or a table;
;; `e' visits an Experiment's config file; `?' opens a menu of every
;; action the triage CLI offers.
;;
;; Emacs never connects to PostgreSQL and never reads a config.  It
;; starts the triage CLI asynchronously and draws the JSON the CLI
;; prints, so every number on screen also comes out of a CLI verb.
;;
;; This file holds what every buffer needs: project detection, the
;; triage executable, the process environment and the asynchronous
;; JSON call.  The buffers and menus live in triage-status.el,
;; triage-object.el and triage-transient.el.

;;; Code:

(require 'cl-lib)
(require 'seq)
(require 'subr-x)
(require 'ansi-color)
(require 'compile)
(require 'magit-section)
(require 'tabulated-list)

;;;; Options

(defgroup triage nil
  "A front end for triage-pg projects."
  :group 'tools
  :prefix "triage-"
  :link '(url-link "https://ccd-ia.github.io/triage-pg/reference/emacs/"))

(defcustom triage-project-markers
  (list #'triage-pyproject-names-triage-p "database.yaml")
  "What marks the root directory of a triage-pg project.
Each element is a file name, which marks a directory that contains
it, or a function, which is called with a directory and returns
non-nil when that directory is a root.  The nearest directory above
the current one that matches any element is the root."
  :type '(repeat (choice (string :tag "File name")
                         (function :tag "Predicate on a directory")))
  :group 'triage)

(defcustom triage-executable nil
  "How to start the triage CLI.
nil means find it: $UV_PROJECT_ENVIRONMENT/bin/triage, then
ROOT/.venv/bin/triage, then triage on the variable `exec-path',
then \"uv run triage\".  A string is the path of the executable.
A list of strings is a command prefix, such as
\\(\"uv\" \"run\" \"triage\")."
  :type '(choice (const :tag "Find it" nil)
                 (string :tag "Executable")
                 (repeat :tag "Command prefix" string))
  :group 'triage)

(defcustom triage-show-archived nil
  "Non-nil means the status buffer lists archived Experiments too."
  :type 'boolean
  :group 'triage)

(defcustom triage-table-limit 200
  "How many rows the table browser reads from a relation."
  :type 'natnum
  :group 'triage)

;;;; Faces

(defface triage-danger '((t :inherit error))
  "Face for errors and an unreachable database."
  :group 'triage)

(defface triage-warning '((t :inherit warning))
  "Face for warnings and pending work."
  :group 'triage)

(defface triage-ok '((t :inherit success))
  "Face for a reachable database and finished work."
  :group 'triage)

(defface triage-dim '((t :inherit shadow))
  "Face for secondary text such as hashes and paths."
  :group 'triage)

(defface triage-key '((t :inherit font-lock-variable-name-face))
  "Face for the labels of key-value lines."
  :group 'triage)

;;;; The contract

(defconst triage--contract 1
  "The version of the CLI's JSON contract this triage.el reads.
The CLI prints it as \"contract\" in status --full and experiment
show.  It is bumped on every breaking change to that JSON.")

(defconst triage--min-version "1.6.0"
  "The first triage-pg release whose CLI prints the JSON this file reads.")

(defvar triage--call-function #'triage--start-process
  "The function that starts a triage process.
It is called with the keyword arguments :command, :directory,
:buffer, :stderr-buffer and :finish.  It inserts the process's
standard output into :buffer and its standard error into
:stderr-buffer, and calls :finish with the exit code once both
streams are closed.  ERT replaces it with a stub that inserts
fixture text, so the tests start no process.")

;;;; Buffer-local state

(defvar-local triage--root nil
  "The project root this buffer belongs to.")

(defvar-local triage--status-buffer nil
  "The status buffer an object view was opened from.")

(defvar triage--root-cache (make-hash-table :test #'equal)
  "Project roots found so far, keyed by directory.
A directory with no root maps to the symbol `none'.")

(defvar triage--executable-cache (make-hash-table :test #'equal)
  "Command prefixes resolved so far, keyed by project root.")

(defun triage-clear-caches ()
  "Forget every cached project root and executable."
  (interactive)
  (clrhash triage--root-cache)
  (clrhash triage--executable-cache))

;;;; Project detection

(defun triage-pyproject-names-triage-p (dir)
  "Return non-nil if DIR has a pyproject.toml naming triage-pg.
The distribution is called triage, so a dependency names the
repository instead: \"triage @ git+https://github.com/ccd-ia/
triage-pg.git@v1.6.0\", or a uv source path such as ../triage-pg.
A line matches when triage-pg follows a slash or a quote and is
not the start of a longer name.  Comment lines are skipped, so a
project that only mentions triage-pg in a comment is not a root."
  (let ((file (expand-file-name "pyproject.toml" dir)))
    (and (file-readable-p file)
         (with-temp-buffer
           (insert-file-contents file)
           (let ((found nil))
             (while (and (not found)
                         (re-search-forward
                          "[/\"']triage-pg\\(?:[^[:alnum:]_-]\\|$\\)" nil t))
               (setq found (not (save-excursion
                                  (goto-char (match-beginning 0))
                                  (beginning-of-line)
                                  (looking-at-p "[ \t]*#")))))
             found)))))

(defun triage--marker-matches-p (marker dir)
  "Return non-nil if DIR is a project root by MARKER."
  (if (functionp marker)
      (funcall marker dir)
    (file-exists-p (expand-file-name marker dir))))

(defun triage-project-root (&optional dir)
  "Return the root of the triage-pg project around DIR.
DIR defaults to `default-directory'.  The root is the nearest
directory at or above DIR that matches `triage-project-markers'.
Return nil when there is none.  Results are cached, and
`triage-clear-caches' forgets them."
  (let* ((dir (file-name-as-directory (expand-file-name (or dir default-directory))))
         (cached (gethash dir triage--root-cache)))
    (if cached
        (and (not (eq cached 'none)) cached)
      (let ((root (locate-dominating-file
                   dir (lambda (d)
                         (seq-some (lambda (m) (triage--marker-matches-p m d))
                                   triage-project-markers)))))
        (setq root (and root (file-name-as-directory (expand-file-name root))))
        (puthash dir (or root 'none) triage--root-cache)
        root))))

(defun triage--describe-markers ()
  "Describe `triage-project-markers' for an error message."
  (mapconcat (lambda (m)
               (cond ((eq m #'triage-pyproject-names-triage-p)
                      "a pyproject.toml naming triage-pg")
                     ((functionp m) (format "%s" m))
                     (t m)))
             triage-project-markers ", "))

(defun triage--require-root (&optional dir)
  "Return the project root above DIR or signal a `user-error'."
  (let ((dir (or dir default-directory)))
    (or (triage-project-root dir)
        (user-error "No triage project above %s (looked for: %s)"
                    (abbreviate-file-name dir) (triage--describe-markers)))))

(defun triage--project-name (root)
  "Return the name of the project at ROOT, from its directory name."
  (file-name-nondirectory (directory-file-name root)))

;;;; The executable

(defun triage--find-executable (root)
  "Return the command prefix to start triage for ROOT.
Follows the order documented in `triage-executable'.  Reads the
environment and variable `exec-path' of the current buffer."
  (cond
   ((stringp triage-executable) (list triage-executable))
   ((consp triage-executable) triage-executable)
   (t
    (let ((uv-env (getenv "UV_PROJECT_ENVIRONMENT")))
      (or (and uv-env (not (string-empty-p uv-env))
               (let ((exe (expand-file-name "bin/triage" (expand-file-name uv-env root))))
                 (and (file-executable-p exe) (list exe))))
          (let ((exe (expand-file-name ".venv/bin/triage" root)))
            (and (file-executable-p exe) (list exe)))
          (let ((exe (executable-find "triage")))
            (and exe (list exe)))
          (list "uv" "run" "triage"))))))

(defun triage--executable (&optional root)
  "Return the cached command prefix to start triage for ROOT.
ROOT defaults to the current buffer's project root."
  (let ((root (or root triage--root (triage--require-root))))
    (or (gethash root triage--executable-cache)
        (puthash root (triage--find-executable root) triage--executable-cache))))

(defun triage--describe-executable (root)
  "Return the executable for ROOT as text, relative to ROOT when inside it."
  (let ((cmd (triage--executable root)))
    (mapconcat (lambda (part)
                 (if (and (file-name-absolute-p part)
                          (string-prefix-p root part))
                     (file-relative-name part root)
                   (abbreviate-file-name part)))
               cmd " ")))

;;;; Buffers that belong to a project

(defun triage--capture (&optional origin root)
  "Return the project state of ORIGIN for `triage--restore'.
ORIGIN defaults to the current buffer.  The state holds its
buffer-local `process-environment' and variable `exec-path',
which envrc.el sets, so processes started from another buffer see
the same PG* variables.  ROOT overrides the project root of
ORIGIN."
  (let ((origin (or origin (current-buffer))))
    (with-current-buffer origin
      (list :env (copy-sequence process-environment)
            :path (copy-sequence exec-path)
            :root (or root triage--root)
            :status (if (derived-mode-p 'triage-status-mode)
                        origin
                      triage--status-buffer)))))

(defun triage--restore (state)
  "Make the current buffer carry STATE from `triage--capture'.
Call this after setting the major mode, which clears local
variables."
  (setq-local process-environment (copy-sequence (plist-get state :env)))
  (setq-local exec-path (copy-sequence (plist-get state :path)))
  (setq-local triage--root (plist-get state :root))
  (setq-local triage--status-buffer (plist-get state :status))
  (setq default-directory (plist-get state :root)))

(defmacro triage--with-global-env (&rest body)
  "Run BODY with this buffer's environment as the global one.
Commands such as `vterm' start their process from a new buffer,
which would not see a buffer-local `process-environment'."
  (declare (indent 0) (debug t))
  (let ((env (gensym "env")) (path (gensym "path")) (dir (gensym "dir")))
    `(let ((,env process-environment) (,path exec-path) (,dir default-directory))
       (with-temp-buffer
         (let ((process-environment ,env)
               (exec-path ,path)
               (default-directory ,dir))
           ,@body)))))

;;;; Errors

(defconst triage-errors-buffer "*triage-errors*"
  "The buffer that collects the standard error of failed calls.")

(defun triage--last-line (text)
  "Return the last non-blank line of TEXT, or nil."
  (car (last (seq-remove #'string-blank-p (split-string (or text "") "\n")))))

(defun triage--log-error (command code text)
  "Append a failed COMMAND with exit CODE and its output TEXT to the log."
  (with-current-buffer (get-buffer-create triage-errors-buffer)
    (let ((inhibit-read-only t))
      (goto-char (point-max))
      (insert (format-time-string "[%F %T] ")
              (mapconcat #'shell-quote-argument command " ")
              (format " (exit %s)\n" code)
              (string-trim-right (or text "")) "\n\n"))
    (special-mode)))

(defun triage--report-error (command code text errback)
  "Report that COMMAND failed with exit CODE and output TEXT.
The full TEXT goes to `triage-errors-buffer'.  ERRBACK, when
non-nil, is called with the last line of TEXT; otherwise that
line is shown in the echo area."
  (triage--log-error command code text)
  (let ((msg (or (triage--last-line text) (format "triage exited with code %s" code))))
    (if errback
        (funcall errback msg)
      (message "triage: %s (see %s)" msg triage-errors-buffer))))

;;;; Processes

(defun triage--start-process (&rest args)
  "Start a triage process as `triage--call-function' describes.
ARGS are the keyword arguments :command, :directory, :buffer,
:stderr-buffer and :finish."
  (let* ((command (plist-get args :command))
         (default-directory (plist-get args :directory))
         (finish (plist-get args :finish))
         (code nil)
         (stderr-closed nil)
         (done (lambda ()
                 (when (and code stderr-closed)
                   (funcall finish code))))
         (stderr (make-pipe-process
                  :name "triage-stderr"
                  :buffer (plist-get args :stderr-buffer)
                  :noquery t
                  :sentinel (lambda (_proc _event)
                              (unless stderr-closed
                                (setq stderr-closed t)
                                (funcall done))))))
    (make-process
     :name "triage"
     :buffer (plist-get args :buffer)
     :stderr stderr
     :command command
     :noquery t
     :connection-type 'pipe
     :sentinel (lambda (proc _event)
                 (unless (process-live-p proc)
                   (setq code (process-exit-status proc))
                   (funcall done))))))

(defun triage--call (args callback)
  "Run triage with ARGS asynchronously and call CALLBACK when it ends.
CALLBACK receives the exit code, the standard output and the
standard error as strings.  Return immediately."
  (let* ((root (or triage--root (triage--require-root)))
         (command (append (triage--executable root) args))
         (out (generate-new-buffer " *triage-out*"))
         (err (generate-new-buffer " *triage-err*")))
    (funcall triage--call-function
             :command command
             :directory root
             :buffer out
             :stderr-buffer err
             :finish (lambda (code)
                       (let ((stdout (with-current-buffer out (buffer-string)))
                             (stderr (with-current-buffer err (buffer-string))))
                         (kill-buffer out)
                         (kill-buffer err)
                         (funcall callback code stdout stderr))))))

(defun triage--parse-json (text)
  "Parse TEXT as JSON into alists and lists, or return the symbol `invalid'."
  (condition-case nil
      (json-parse-string text
                         :object-type 'alist :array-type 'list
                         :null-object nil :false-object :false)
    (json-error 'invalid)))

(defun triage--contract-problem (payload require-contract)
  "Return a message when PAYLOAD does not match this file's contract.
REQUIRE-CONTRACT non-nil means PAYLOAD must carry a contract key."
  (let ((contract (and (consp payload) (consp (car payload))
                       (alist-get 'contract payload))))
    (cond
     ((and contract (> contract triage--contract))
      (format "triage-pg is newer than triage.el (contract %s > %s); update triage.el"
              contract triage--contract))
     ((and require-contract (null contract))
      (format "triage-pg >= %s needed (%s missing)" triage--min-version
              require-contract)))))

(cl-defun triage--call-json (args callback &key errback require-contract)
  "Run triage with ARGS asynchronously and call CALLBACK with its JSON.
The payload is parsed into alists (symbol keys) and lists, with
JSON null as nil and false as :false.  On a non-zero exit, or when
the output is not JSON, the error goes to `triage--report-error'
and ERRBACK (or the echo area) gets its last line.  When the
payload's contract is newer than this file's, or REQUIRE-CONTRACT
\(a description of the call) is non-nil and the payload has none,
ERRBACK gets a message that names the side to update.  Return
immediately."
  (let ((command (append (triage--executable) args)))
    (triage--call
     args
     (lambda (code stdout stderr)
       (if (not (zerop code))
           (triage--report-error command code stderr errback)
         (let ((payload (triage--parse-json stdout)))
           (if (eq payload 'invalid)
               ;; `triage query --json' prints a failed statement's error on
               ;; standard output and exits 0, so the output is the message.
               (triage--report-error command code
                                     (if (string-blank-p stdout) stderr stdout)
                                     errback)
             (let ((problem (triage--contract-problem payload require-contract)))
               (if problem
                   (progn (triage--log-error command code problem)
                          (if errback (funcall errback problem)
                            (message "triage: %s" problem)))
                 (funcall callback payload))))))))))

;;;; Long-running verbs

(defvar-local triage--process-args nil
  "The triage arguments of the process this buffer shows.")

(defvar-local triage--process-on-exit nil
  "The function this buffer's process calls when it ends.")

(defvar-local triage--process-on-match nil
  "A (REGEXP . FUNCTION) pair: FUNCTION runs once on the first match.")

(defvar triage-process-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "g") #'triage-process-rerun)
    map)
  "Keymap for `triage-process-mode'.")

(define-derived-mode triage-process-mode compilation-mode "Triage-Process"
  "Mode for the output of a long-running triage verb.
\\<triage-process-mode-map>\\[triage-process-rerun] runs the verb again.
\\[kill-compilation] stops it."
  (setq-local compilation-error-regexp-alist nil)
  (add-hook 'compilation-filter-hook #'ansi-color-compilation-filter nil t))

(defun triage--process-filter (proc text)
  "Insert TEXT from PROC and watch for the buffer's match."
  (compilation-filter proc text)
  (when-let* ((buffer (process-buffer proc))
              ((buffer-live-p buffer)))
    (with-current-buffer buffer
      (when-let* ((watch triage--process-on-match)
                  ((string-match (car watch) text)))
        (setq triage--process-on-match nil)
        (funcall (cdr watch) (match-string 1 text))))))

(defun triage--process-sentinel (proc _event)
  "Report the end of PROC in its buffer and run its exit function."
  (unless (process-live-p proc)
    (let ((buffer (process-buffer proc))
          (code (process-exit-status proc)))
      (when (buffer-live-p buffer)
        (with-current-buffer buffer
          (let ((inhibit-read-only t))
            (goto-char (point-max))
            (insert (propertize (format "\ntriage finished with exit code %d at %s\n"
                                        code (format-time-string "%T"))
                                'face (if (zerop code) 'triage-ok 'triage-danger))))
          (when triage--process-on-exit
            (funcall triage--process-on-exit code)))))))

(defvar triage--process-starter #'make-process
  "The function `triage--run-in-buffer' starts processes with.
ERT replaces it to check the command without starting anything.")

(cl-defun triage--run-in-buffer (name args &key on-exit on-match)
  "Run triage ARGS in the process buffer *triage: NAME* and show it.
The buffer keeps the output with its ANSI colours.  There is one
buffer per NAME.  ON-EXIT, when non-nil, is called with the exit
code in that buffer when the process ends.  ON-MATCH is a
\(REGEXP . FUNCTION) pair: FUNCTION is called once, with the first
group of REGEXP, when the output first matches.  Return the buffer."
  (let* ((root (or triage--root (triage--require-root)))
         (command (append (triage--executable root) args))
         (buffer (get-buffer-create (format "*triage: %s*" name)))
         (state (triage--capture nil root)))
    (when-let* ((proc (get-buffer-process buffer))
                ((process-live-p proc)))
      (unless (yes-or-no-p (format "The triage %s process is still running; stop it and start again? " name))
        (user-error "Left the running %s alone" name))
      (delete-process proc))
    (with-current-buffer buffer
      (let ((inhibit-read-only t))
        (erase-buffer)
        (triage-process-mode)
        (triage--restore state)
        (insert (propertize (format "%s\n\n" (mapconcat #'shell-quote-argument command " "))
                            'face 'triage-dim)))
      (setq triage--process-args args
            triage--process-on-exit on-exit
            triage--process-on-match on-match)
      (let ((proc (funcall triage--process-starter
                           :name (format "triage %s" name)
                           :buffer buffer
                           :command command
                           :noquery t
                           :connection-type 'pipe
                           :filter #'triage--process-filter
                           :sentinel #'triage--process-sentinel)))
        (when (processp proc)
          (set-marker (process-mark proc) (point-max) buffer))))
    (display-buffer buffer)
    buffer))

(defun triage-process-rerun ()
  "Run this buffer's triage verb again."
  (interactive)
  (unless triage--process-args
    (user-error "This buffer has no triage command to run again"))
  (triage--run-in-buffer
   (string-remove-suffix "*" (string-remove-prefix "*triage: " (buffer-name)))
   triage--process-args
   :on-exit triage--process-on-exit))

;;;; Small helpers for the views

(defun triage--get (alist &rest keys)
  "Follow KEYS through nested ALIST and return the value."
  (let ((value alist))
    (dolist (key keys value)
      (setq value (and (consp value) (alist-get key value))))))

(defun triage--str (value)
  "Return VALUE as display text."
  (cond ((null value) "")
        ((eq value :false) "false")
        ((eq value t) "true")
        ((stringp value) value)
        ((floatp value) (format "%.4f" value))
        ((numberp value) (number-to-string value))
        (t (prin1-to-string value))))

(defun triage--short (hash)
  "Return the first 12 characters of HASH."
  (if (and (stringp hash) (> (length hash) 12)) (substring hash 0 12) (or hash "")))

(defun triage--time (timestamp)
  "Return TIMESTAMP as YYYY-MM-DD HH:MM, as the CLI wrote it."
  (if (and (stringp timestamp) (>= (length timestamp) 16))
      (string-replace "T" " " (substring timestamp 0 16))
    (or timestamp "")))

(defun triage--pad (text width)
  "Return TEXT truncated or padded to WIDTH columns."
  (truncate-string-to-width (or text "") width nil ?\s "…"))

(defun triage--key-line (label value &optional face)
  "Insert a line with LABEL and VALUE, VALUE in FACE."
  (insert (propertize (format "%-18s" (concat label ":")) 'font-lock-face 'triage-key)
          (if face (propertize (triage--str value) 'font-lock-face face) (triage--str value))
          "\n"))

;;;; Section buffers

(declare-function triage-dispatch "triage-transient" ())
(declare-function triage-run-menu "triage-transient" ())
(declare-function triage-check-menu "triage-transient" ())
(declare-function triage-analyze-menu "triage-transient" ())
(declare-function triage-results-menu "triage-transient" ())
(declare-function triage-score-menu "triage-transient" ())
(declare-function triage-plot-menu "triage-transient" ())
(declare-function triage-maintain-menu "triage-transient" ())
(declare-function triage-serve-menu "triage-transient" ())
(declare-function triage-actions "triage-transient" ())
(declare-function triage-tui "triage-transient" ())
(declare-function triage-query "triage-object" (sql))

(defvar-local triage--refresh-function nil
  "The function \\[triage-refresh] calls in this buffer.")

(defvar-local triage--items nil
  "The data behind each section of this buffer.
A hash table from (TYPE . VALUE) of a section to its data.")

(defvar-local triage--context nil
  "The items this buffer is about.
A plist of :config, :hash, :run, :model and :group.  The menus
read it when the section at point names nothing.")

(defvar-local triage--visit-functions nil
  "An alist from section type to the function RET calls on it.
The function receives the section's value and its data.")

(defvar-local triage--edit-functions nil
  "An alist from section type to the function \\`e' calls on it.
The function receives the section's value and its data.")

(defvar triage-section-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "g") #'triage-refresh)
    (define-key map (kbd "RET") #'triage-visit)
    (define-key map (kbd "e") #'triage-edit)
    (define-key map (kbd "?") #'triage-dispatch)
    (define-key map (kbd "h") #'triage-dispatch)
    (define-key map (kbd "r") #'triage-run-menu)
    (define-key map (kbd "c") #'triage-check-menu)
    (define-key map (kbd "a") #'triage-analyze-menu)
    (define-key map (kbd "l") #'triage-results-menu)
    (define-key map (kbd "f") #'triage-score-menu)
    (define-key map (kbd "p") #'triage-plot-menu)
    (define-key map (kbd "m") #'triage-maintain-menu)
    (define-key map (kbd "s") #'triage-serve-menu)
    (define-key map (kbd ":") #'triage-query)
    (define-key map (kbd "!") #'triage-actions)
    (define-key map (kbd "T") #'triage-tui)
    (define-key map (kbd "q") #'quit-window)
    map)
  "Keymap shared by every triage section buffer.")

(define-derived-mode triage-section-mode magit-section-mode "Triage"
  "Parent mode of the triage buffers that fold into sections.

\\{triage-section-mode-map}"
  :group 'triage
  (setq-local revert-buffer-function (lambda (&rest _) (triage-refresh)))
  (setq triage--items (make-hash-table :test #'equal)))

(defun triage-refresh ()
  "Read this buffer's data again from the triage CLI."
  (interactive)
  (if triage--refresh-function
      (funcall triage--refresh-function)
    (user-error "Nothing to refresh here")))

(defun triage--remember (type value data)
  "Record DATA as the data behind the section of TYPE and VALUE."
  (puthash (cons type value) data triage--items))

(defun triage--section-data (section)
  "Return the data recorded for SECTION."
  (and section triage--items
       (gethash (cons (oref section type) (oref section value)) triage--items)))

(defun triage--call-section-function (alist what)
  "Call the function ALIST maps the section at point to.
When there is none, signal that there is nothing to WHAT."
  (let ((section (magit-current-section)) fn)
    (while (and section (not (setq fn (alist-get (oref section type) alist))))
      (setq section (oref section parent)))
    (if fn
        (funcall fn (oref section value) (triage--section-data section))
      (user-error "Nothing to %s at point" what))))

(defun triage-visit ()
  "Open the item at point: an Experiment, Run, model, table or source."
  (interactive)
  (triage--call-section-function triage--visit-functions "open"))

(defun triage-edit ()
  "Visit the config file of the Experiment at point."
  (interactive)
  (triage--call-section-function triage--edit-functions "edit"))

(defun triage--at-point (kind)
  "Return the KIND of item at point, or nil.
KIND is one of :config (a config path relative to the root),
:hash (an experiment_hash), :run (a run id), :model (a model id)
or :group (a model group id).  The section at point and its
parents are asked first, then the buffer's `triage--context', then
a config file the current buffer visits."
  (or (let ((section (and (derived-mode-p 'magit-section-mode) (magit-current-section)))
            found)
        (while (and section (not found))
          (setq found (plist-get (triage--section-data section) kind))
          (setq section (oref section parent)))
        found)
      (and triage--row-context-function (tabulated-list-get-id)
           (plist-get (funcall triage--row-context-function (tabulated-list-get-id)) kind))
      (plist-get triage--context kind)
      (and (eq kind :config) buffer-file-name
           (string-match-p "\\.ya?ml\\'" buffer-file-name)
           (let ((root (triage-project-root)))
             (and root (file-relative-name buffer-file-name root))))))

(defvar-local triage--payload nil
  "The JSON payload an object view was drawn from.")

(defvar-local triage--loading nil
  "Non-nil while this buffer waits for the triage CLI.")

(defvar-local triage--load-error nil
  "The message of this buffer's last failed call, or nil.")

(defvar-local triage--row-context-function nil
  "In a table view, a function from a row to its context plist.")

(defvar triage-status--payload)

(defun triage--redraw (type insert-function)
  "Erase the buffer and call INSERT-FUNCTION inside a root section of TYPE.
Keep point on the same section, and keep every section folded or
expanded as it was."
  (let* ((inhibit-read-only t)
         (section (magit-current-section))
         (ident (and section (magit-section-ident section)))
         (line (line-number-at-pos)))
    (clrhash triage--items)
    (erase-buffer)
    (magit-insert-section ((eval type))
      (funcall insert-function))
    (magit-section-show magit-root-section)
    (let ((target (and ident (magit-get-section ident))))
      (if target
          (goto-char (oref target start))
        (goto-char (point-min))
        (forward-line (1- line))))))

(defun triage--status-payload ()
  "Return the status payload of the buffer's project, or nil."
  (let ((status (if (derived-mode-p 'triage-status-mode) (current-buffer) triage--status-buffer)))
    (and status (buffer-live-p status)
         (buffer-local-value 'triage-status--payload status))))

(defun triage--candidates (kind)
  "Return an alist of (LABEL . VALUE) for KIND that a prompt can offer.
KIND is a key of `triage--at-point'.  Configs, Experiments and
Runs come from the status payload, model groups and models from
the payload of the current view."
  (let ((status (triage--status-payload))
        (payload triage--payload))
    (pcase kind
      (:config (mapcar (lambda (c) (cons (alist-get 'path c) (alist-get 'path c)))
                       (seq-remove (lambda (c) (alist-get 'error c))
                                   (alist-get 'configs status))))
      (:hash (seq-uniq
              (append
               (mapcar (lambda (e) (cons (format "%s  %s" (triage--short (alist-get 'experiment_hash e))
                                                 (triage--str (alist-get 'name e)))
                                         (alist-get 'experiment_hash e)))
                       (alist-get 'experiments status))
               (mapcar (lambda (c) (cons (format "%s  %s" (triage--short (alist-get 'experiment_hash c))
                                                 (alist-get 'path c))
                                         (alist-get 'experiment_hash c)))
                       (seq-filter (lambda (c) (alist-get 'experiment_hash c))
                                   (alist-get 'configs status))))
              (lambda (a b) (equal (cdr a) (cdr b)))))
      (:run (mapcar (lambda (r) (cons (format "%s  %s  %s" (alist-get 'run_id r)
                                              (triage--str (alist-get 'name r))
                                              (triage--str (alist-get 'state r)))
                                      (alist-get 'run_id r)))
                    (alist-get 'last_runs status)))
      (:group (mapcar (lambda (g) (let ((id (number-to-string (alist-get 'model_group_id g))))
                                    (cons (format "%s  %s" id (triage--str (alist-get 'model_type g)))
                                          id)))
                      (triage--get payload 'history 'model_groups)))
      (:model (mapcar (lambda (row) (let ((id (number-to-string (alist-get 'model_id row))))
                                      (cons (format "%s  group %s  %s" id
                                                    (alist-get 'model_group_id row)
                                                    (triage--str (alist-get 'as_of_date row)))
                                            id)))
                      (triage--get payload 'history 'leaderboard))))))

(defun triage--read-target (kind prompt)
  "Return the KIND at point, or read one with PROMPT from its candidates.
Signal a `user-error' when there is neither."
  (or (let ((value (triage--at-point kind)))
        (if (numberp value) (number-to-string value) value))
      (let ((candidates (triage--candidates kind)))
        (cond
         (candidates
          (let ((choice (completing-read prompt candidates nil t)))
            (cdr (assoc choice candidates))))
         ((eq kind :config)
          (file-relative-name (read-file-name prompt nil nil t) triage--root))
         (t (user-error "No %s at point or in this project's buffers"
                        (substring (symbol-name kind) 1)))))))

(defun triage--has-target-p (kind)
  "Return non-nil if a KIND is at point or a prompt can offer one."
  (or (triage--at-point kind) (eq kind :config) (triage--candidates kind)))

(defun triage--refresh-status-buffer (&optional _code)
  "Refresh the status buffer this buffer belongs to, if it is live."
  (let ((status (if (derived-mode-p 'triage-status-mode)
                    (current-buffer)
                  triage--status-buffer)))
    (when (and status (buffer-live-p status))
      (with-current-buffer status
        (when (fboundp 'triage-status-refresh)
          (triage-status-refresh))))))

(provide 'triage)

(cl-eval-when (load eval)
  (require 'triage-status)
  (require 'triage-object)
  (require 'triage-transient))

;;; triage.el ends here
