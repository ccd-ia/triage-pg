;;; ci.el --- Byte-compile, checkdoc and ERT for triage.el  -*- lexical-binding: t; -*-

;;; Commentary:

;; The one entry point for the Elisp checks, used by CI and by
;; `just emacs-test':
;;
;;   emacs --batch -l emacs/test/ci.el
;;
;; It installs magit-section and transient from GNU ELPA and NonGNU
;; ELPA into a temporary package directory, byte-compiles every
;; emacs/*.el with warnings as errors, runs checkdoc on each file and
;; fails on any message, then runs the ERT suite in triage-test.el.
;; TRIAGE_EL_PACKAGE_DIR names a package directory to reuse between
;; local runs instead of a fresh temporary one.

;;; Code:

(require 'package)
(require 'bytecomp)
(require 'checkdoc)
(require 'seq)
(require 'subr-x)

(defconst triage-ci--test-dir
  (file-name-directory (or load-file-name buffer-file-name)))

(defconst triage-ci--lisp-dir
  (expand-file-name "../" triage-ci--test-dir))

(defconst triage-ci--sources
  (directory-files triage-ci--lisp-dir t "\\`triage.*\\.el\\'"))

(defun triage-ci--fail (format-string &rest args)
  "Print FORMAT-STRING with ARGS to stderr and exit with code 1."
  (message "FAIL: %s" (apply #'format format-string args))
  (kill-emacs 1))

;;;; Dependencies

(setq package-user-dir
      (let ((dir (getenv "TRIAGE_EL_PACKAGE_DIR")))
        (if (and dir (not (string-empty-p dir)))
            (expand-file-name dir)
          (let ((temp (make-temp-file "triage-el-elpa-" t)))
            (add-hook 'kill-emacs-hook (lambda () (delete-directory temp t)))
            temp))))
(setq package-archives '(("gnu" . "https://elpa.gnu.org/packages/")
                         ("nongnu" . "https://elpa.nongnu.org/nongnu/")))
(package-initialize)

(defun triage-ci--installed-version (package)
  "Return the version list of PACKAGE installed in `package-user-dir'."
  (when-let* ((desc (cadr (assq package package-alist))))
    (package-desc-version desc)))

;; Emacs 29 ships transient 0.4, older than the 0.7 the package needs,
;; so transient is installed from GNU ELPA even when one is built in.
(let ((missing (seq-remove #'triage-ci--installed-version '(magit-section transient))))
  (when missing
    (package-refresh-contents)
    (dolist (pkg missing)
      (let ((desc (cadr (assq pkg package-archive-contents))))
        (unless desc
          (triage-ci--fail "%s is not in GNU ELPA or NonGNU ELPA" pkg))
        (package-install desc)))))
(package-activate-all)
(require 'transient)
(require 'magit-section)
(message "Using transient %s and magit-section %s from %s"
         (package-version-join (triage-ci--installed-version 'transient))
         (package-version-join (triage-ci--installed-version 'magit-section))
         package-user-dir)

(add-to-list 'load-path triage-ci--lisp-dir)

;;;; Byte-compile

(let ((out-dir (make-temp-file "triage-el-elc-" t))
      (byte-compile-error-on-warn t)
      (failed nil))
  (let ((byte-compile-dest-file-function
         (lambda (source)
           (expand-file-name (concat (file-name-nondirectory source) "c") out-dir))))
    (dolist (file triage-ci--sources)
      (unless (byte-compile-file file)
        (push (file-name-nondirectory file) failed))))
  (delete-directory out-dir t)
  (when failed
    (triage-ci--fail "byte-compile: %s" (string-join (nreverse failed) ", ")))
  (message "byte-compile: %d files, 0 warnings" (length triage-ci--sources)))

;;;; Checkdoc

(let ((messages nil))
  (advice-add 'checkdoc-error :before
              (lambda (point msg)
                (push (format "%s:%s: %s" (buffer-name)
                              (count-lines (point-min) (or point (point-min))) msg)
                      messages)))
  (dolist (file triage-ci--sources)
    (checkdoc-file file))
  (when messages
    (setq messages (nreverse messages))
    (dolist (m messages) (message "checkdoc: %s" m))
    (triage-ci--fail "checkdoc: %d messages" (length messages)))
  (message "checkdoc: %d files, 0 messages" (length triage-ci--sources)))

;;;; ERT

(load (expand-file-name "triage-test.el" triage-ci--test-dir) nil t)
(ert-run-tests-batch-and-exit)

;;; ci.el ends here
