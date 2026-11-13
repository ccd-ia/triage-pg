;;; triage-status.el --- The triage status buffer  -*- lexical-binding: t; -*-

;; Copyright (c) 2026 Adolfo De Unánue and triage-pg contributors
;; SPDX-License-Identifier: MIT

;; This file is not part of GNU Emacs.

;;; Commentary:

;; `triage-status' shows one Project: a header with the database
;; state, then the pending work, the Experiments matched to their
;; config files, the recent Runs, the declared Sources and the tables
;; of the triage schema.  A refresh is one asynchronous call to
;; triage status --json --full.

;;; Code:

(require 'triage)

(declare-function triage-experiment-show "triage-object" (target))
(declare-function triage-run-show "triage-object" (run-id))
(declare-function triage-table-show "triage-object" (name))
(declare-function triage-source-show "triage-object" (source))

(defvar-local triage-status--payload nil
  "The last status --json --full payload this buffer received.")

(defvar-local triage-status--error nil
  "The message of the last failed refresh, or nil.")

(defvar-local triage-status--refreshing nil
  "Non-nil while a refresh is in flight.")

(defvar triage-status-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "A") #'triage-status-toggle-archived)
    map)
  "Keymap for `triage-status-mode'.")

(define-derived-mode triage-status-mode triage-section-mode "Triage-Status"
  "Mode for the status buffer of a triage-pg project.

\\{triage-status-mode-map}"
  :group 'triage
  (setq mode-line-process '(triage-status--refreshing " refreshing…"))
  (setq triage--refresh-function #'triage-status-refresh)
  (setq triage--visit-functions
        `((triage-experiment . ,#'triage-status--visit-experiment)
          (triage-config . ,#'triage-status--visit-config)
          (triage-run . ,(lambda (id _data) (triage-run-show id)))
          (triage-relation . ,(lambda (name _data) (triage-table-show name)))
          (triage-source . ,(lambda (_name data) (triage-source-show (plist-get data :row))))))
  (setq triage--edit-functions
        `((triage-experiment . ,#'triage-status--edit-experiment)
          (triage-config . ,(lambda (path _data) (triage--find-config path))))))

;;;; Entry point

;;;###autoload
(defun triage-status (&optional dir)
  "Show the status of the triage-pg project around DIR.
DIR defaults to `default-directory'.  With a prefix argument, forget
the cached project roots and executables first.  The buffer is
*triage: PROJECT*, reused when it exists.  It fills in when the
triage CLI answers; this command returns at once."
  (interactive (progn (when current-prefix-arg (triage-clear-caches)) nil))
  (let* ((root (triage--require-root dir))
         (state (triage--capture nil root))
         (buffer (get-buffer-create (format "*triage: %s*" (triage--project-name root)))))
    (with-current-buffer buffer
      (unless (derived-mode-p 'triage-status-mode)
        (triage-status-mode))
      (triage--restore (plist-put state :status buffer))
      (unless triage-status--payload
        (triage-status--render))
      (triage-status-refresh))
    (pop-to-buffer-same-window buffer)
    buffer))

;;;###autoload
(defalias 'triage #'triage-status)

;;;; Refresh

(defun triage-status-refresh ()
  "Read the project's status again: one triage status --json --full call."
  (interactive)
  (if triage-status--refreshing
      (message "triage: a refresh is already running")
    (let ((buffer (current-buffer)))
      (setq triage-status--refreshing t)
      (force-mode-line-update)
      (triage--call-json
       '("status" "--json" "--full")
       (lambda (payload) (triage-status--receive buffer payload nil))
       :errback (lambda (message) (triage-status--receive buffer nil message))
       :require-contract "status --full"))))

(defun triage-status--receive (buffer payload err)
  "Show PAYLOAD, or the error message ERR, in the status BUFFER."
  (when (buffer-live-p buffer)
    (with-current-buffer buffer
      (setq triage-status--refreshing nil
            triage-status--error err)
      (when payload
        (setq triage-status--payload payload))
      (triage-status--render)
      (force-mode-line-update))))

(defun triage-status-toggle-archived ()
  "Show or hide archived Experiments, without calling the CLI again."
  (interactive)
  (setq-local triage-show-archived (not triage-show-archived))
  (triage-status--render)
  (message "Archived Experiments %s" (if triage-show-archived "shown" "hidden")))

;;;; The experiment and config join

(defun triage-status--experiment-rows (payload)
  "Join PAYLOAD's experiments and configs on experiment_hash.
Return (ROWS . BROKEN).  Each row is a plist with :hash,
:experiment (the database row, or nil when the config has not run)
and :configs (the config entries with that hash).  Archived
Experiments and their files are left out unless
`triage-show-archived' is set.  BROKEN lists the config entries
that failed to parse."
  (let ((configs (alist-get 'configs payload))
        (rows nil)
        (seen (make-hash-table :test #'equal)))
    (cl-flet ((of-hash (hash) (seq-filter (lambda (c) (equal (alist-get 'experiment_hash c) hash))
                                          configs)))
    (dolist (exp (alist-get 'experiments payload))
      (let ((hash (alist-get 'experiment_hash exp)))
        (puthash hash t seen)
        (when (or triage-show-archived (null (alist-get 'archived_at exp)))
          (push (list :hash hash :experiment exp
                      :configs (of-hash hash))
                rows))))
    (dolist (config configs)
      (let ((hash (alist-get 'experiment_hash config)))
        (when (and hash (null (alist-get 'error config)) (not (gethash hash seen)))
          (puthash hash t seen)
          (push (list :hash hash :experiment nil
                      :configs (of-hash hash))
                rows)))))
    (cons (nreverse rows)
          (seq-filter (lambda (c) (alist-get 'error c)) configs))))

;;;; Rendering

(defun triage-status--render ()
  "Draw the status buffer from the last payload, keeping point and folds."
  (let ((payload triage-status--payload))
    (triage--redraw
     'triage-status
     (lambda ()
       (triage-status--insert-header payload)
       (when payload
         (triage-status--insert-pending payload)
         (triage-status--insert-experiments payload)
         (triage-status--insert-runs payload)
         (triage-status--insert-sources payload)
         (triage-status--insert-relations payload))))))

(defun triage-status--insert-header (payload)
  "Insert the header lines for PAYLOAD."
  (let* ((root triage--root)
         (database (alist-get 'database payload))
         (extra (alist-get 'extra payload)))
    (insert (propertize "Project:  " 'font-lock-face 'triage-key)
            (triage--pad (or (alist-get 'project payload)
                             (and root (triage--project-name root)) "")
                         20)
            " ")
    (cond
     ((null payload)
      (insert (propertize (if triage-status--error "" "Loading…") 'font-lock-face 'triage-dim)))
     ((eq (alist-get 'connected database) t)
      (insert (propertize "DB: connected" 'font-lock-face 'triage-ok)
              (mapconcat (lambda (part) (concat " · " part))
                         (seq-remove #'string-empty-p
                                     (list (triage--str (alist-get 'detail database))
                                           (triage--str (alist-get 'schema extra))
                                           (triage--str (alist-get 'size extra))))
                         "")))
     (t
      (insert (propertize (concat "DB: " (triage--str (alist-get 'detail database)))
                          'font-lock-face 'triage-danger))))
    (insert "\n")
    (when root
      (insert (propertize "triage:   " 'font-lock-face 'triage-key)
              (triage--describe-executable root)
              (if payload (format " (%s)" (alist-get 'triage_version payload)) "")
              "\n"))
    (when triage-status--error
      (insert (propertize (format "Error:    %s (see %s)" triage-status--error
                                  triage-errors-buffer)
                          'font-lock-face 'triage-danger)
              "\n"))
    (dolist (warning (alist-get 'warnings payload))
      (insert (propertize (concat "Warning:  " (triage--str warning))
                          'font-lock-face 'triage-warning)
              "\n"))
    (insert "\n")))

(defun triage-status--insert-pending (payload)
  "Insert the Pending section for PAYLOAD."
  (let ((pending (alist-get 'pending payload)))
    (when pending
      (magit-insert-section (triage-pending)
        (magit-insert-heading (format "Pending (%d)" (length pending)))
        (dolist (item pending)
          (let ((name (triage--str (alist-get 'name item)))
                (level (alist-get 'level item)))
            (magit-insert-section (triage-pending-item name)
              (insert "  " (triage--pad name 14) " "
                      (propertize (triage--pad (triage--str (alist-get 'detail item)) 44)
                                  'font-lock-face
                                  (pcase level
                                    ("ok" 'triage-dim)
                                    ("warn" 'triage-warning)
                                    ("error" 'triage-danger)
                                    (_ 'default)))
                      " " (triage--str (alist-get 'due item)) "\n"))))
        (insert "\n")))))

(defun triage-status--runs-text (experiment)
  "Return the run count of EXPERIMENT as text."
  (pcase (alist-get 'n_runs experiment)
    ((or 'nil 0) "—")
    (1 "1 run")
    (n (format "%d runs" n))))

(defun triage-status--insert-experiments (payload)
  "Insert the Experiments section for PAYLOAD."
  (pcase-let* ((`(,rows . ,broken) (triage-status--experiment-rows payload))
               (connected (eq (triage--get payload 'database 'connected) t)))
    (magit-insert-section (triage-experiments)
      (magit-insert-heading (format "Experiments (%d)" (length rows)))
      (dolist (row rows)
        (let* ((hash (plist-get row :hash))
               (exp (plist-get row :experiment))
               (configs (plist-get row :configs))
               (paths (mapcar (lambda (c) (alist-get 'path c)) configs))
               (first-config (car configs))
               (name (or (alist-get 'name exp) (alist-get 'name first-config)
                         (triage--short hash)))
               (problem (or (alist-get 'problem_type exp) (alist-get 'problem_type first-config)))
               (status (cond (exp (or (alist-get 'last_status exp) "—"))
                             (connected "not run")
                             (t "—"))))
          (triage--remember 'triage-experiment hash
                            (list :hash hash :experiment exp :configs configs :paths paths
                                  :config (and (= (length paths) 1) (car paths))))
          (magit-insert-section (triage-experiment hash)
            (insert "  " (propertize (triage--pad name 24) 'font-lock-face 'magit-section-secondary-heading)
                    " " (triage--pad (triage--str problem) 18)
                    " " (triage--pad (if exp (triage-status--runs-text exp) "—") 8)
                    " " (propertize (triage--pad status 10)
                                    'font-lock-face (pcase status
                                                      ("completed" 'triage-ok)
                                                      ("failed" 'triage-danger)
                                                      ("not run" 'triage-dim)
                                                      (_ 'default)))
                    " "
                    (pcase (length paths)
                      (0 (propertize "(no config file)" 'font-lock-face 'triage-dim))
                      (1 (car paths))
                      (n (format "%d config files" n)))
                    (if (and exp (alist-get 'archived_at exp))
                        (propertize "  archived" 'font-lock-face 'triage-dim)
                      "")
                    "\n")
            (when (> (length paths) 1)
              (magit-insert-heading)
              (dolist (config configs)
                (let ((path (alist-get 'path config)))
                  (triage--remember 'triage-config path (list :config path :hash hash))
                  (magit-insert-section (triage-config path)
                    (insert "      " path "\n"))))))))
      (dolist (config broken)
        (let ((path (alist-get 'path config)))
          (triage--remember 'triage-config path (list :error (alist-get 'error config)
                                                      :broken t))
          (magit-insert-section (triage-config path)
            (insert "  " (triage--pad path 52) " "
                    (propertize (triage--str (alist-get 'error config))
                                'font-lock-face 'triage-danger)
                    "\n"))))
      (insert "\n"))))

(defun triage-status--insert-runs (payload)
  "Insert the recent-run section for PAYLOAD."
  (let ((runs (alist-get 'last_runs payload)))
    (magit-insert-section (triage-runs)
      (magit-insert-heading (format "Recent runs (%d)" (length runs)))
      (dolist (run runs)
        (let ((id (alist-get 'run_id run))
              (state (triage--str (alist-get 'state run))))
          (triage--remember 'triage-run id (list :run id :row run))
          (magit-insert-section (triage-run id)
            (insert "  " (propertize (triage--pad (substring id 0 (min 8 (length id))) 9)
                                     'font-lock-face 'triage-dim)
                    " " (triage--pad (triage--str (alist-get 'name run)) 24)
                    " " (propertize (triage--pad state 10)
                                    'font-lock-face (pcase state
                                                      ("succeeded" 'triage-ok)
                                                      ("failed" 'triage-danger)
                                                      ("running" 'triage-warning)
                                                      (_ 'default)))
                    " " (triage--pad (triage--str (alist-get 'detail run)) 14)
                    " " (triage--time (alist-get 'started_at run)) "\n"))))
      (insert "\n"))))

(defun triage-status--insert-sources (payload)
  "Insert the folded Sources section for PAYLOAD."
  (let ((sources (alist-get 'sources payload)))
    (magit-insert-section (triage-sources nil t)
      (magit-insert-heading (format "Sources (%d)" (length sources)))
      (dolist (source sources)
        (let ((name (triage--str (alist-get 'source_name source))))
          (triage--remember 'triage-source name (list :row source))
          (magit-insert-section (triage-source name)
            (insert "  " (triage--pad name 24)
                    " " (triage--pad (triage--str (alist-get 'relation source)) 30)
                    " " (triage--pad (triage--str (alist-get 'version_label source)) 10)
                    " " (triage--time (alist-get 'registered_at source)) "\n"))))
      (insert "\n"))))

(defun triage-status--insert-relations (payload)
  "Insert the folded Tables section for PAYLOAD."
  (let ((relations (alist-get 'relations payload)))
    (magit-insert-section (triage-relations nil t)
      (magit-insert-heading (format "Tables (%d)" (length relations)))
      (dolist (relation relations)
        (let ((name (alist-get 'name relation)))
          (triage--remember 'triage-relation name (list :relation name))
          (magit-insert-section (triage-relation name)
            (insert "  " (triage--pad name 40)
                    " " (propertize (triage--pad (triage--str (alist-get 'kind relation)) 12)
                                    'font-lock-face 'triage-dim)
                    " " (let ((rows (alist-get 'est_rows relation)))
                          (if rows (format "~%s rows" rows) ""))
                    "\n"))))
      (insert "\n"))))

;;;; RET and e

(defun triage--find-config (path)
  "Visit the config file PATH, relative to the project root."
  (let ((root triage--root))
    (find-file (expand-file-name path root))))

(defun triage-status--choose-config (data prompt)
  "Return one config path of the Experiment DATA.
Ask with PROMPT when there are several."
  (let ((paths (plist-get data :paths)))
    (if (cdr paths)
        (completing-read prompt paths nil t)
      (car paths))))

(defun triage-status--visit-experiment (hash data)
  "Open the Experiment HASH, whose status DATA lists its config files.
The view reads the config file when there is one, else the stored
config of HASH."
  (triage-experiment-show
   (or (triage-status--choose-config data "Experiment of which config file? ") hash)))

(defun triage-status--visit-config (path data)
  "Open the Experiment of the config file PATH.
Visit the file instead when its status DATA says it does not parse."
  (if (plist-get data :broken)
      (triage--find-config path)
    (triage-experiment-show path)))

(defun triage-status--edit-experiment (_hash data)
  "Visit the config file of the Experiment whose status DATA lists its files."
  (let ((path (triage-status--choose-config data "Visit config file: ")))
    (if path
        (triage--find-config path)
      (user-error "This Experiment has no config file here; RET shows its stored config"))))

(provide 'triage-status)

;;; triage-status.el ends here
