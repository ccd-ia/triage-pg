;;; triage-object.el --- Experiment, Run, model and table views  -*- lexical-binding: t; -*-

;; Copyright (c) 2026 Adolfo De Unánue and triage-pg contributors
;; SPDX-License-Identifier: MIT

;; This file is not part of GNU Emacs.

;;; Commentary:

;; The views RET opens from the status buffer.  Each one is a single
;; asynchronous call to a CLI verb that prints JSON:
;;
;;   Experiment   triage experiment show TARGET --json
;;   Run          triage runs show ID --json
;;   model group  triage models HASH --group G --json
;;   model        triage model show ID --json
;;   table        triage query --json "select * from triage.NAME limit N"
;;   query        triage query --json SQL
;;
;; The table browser builds the only SQL text in triage.el.  It reads
;; rows of a relation the CLI listed, and the CLI runs it inside a
;; read-only transaction.

;;; Code:

(require 'triage)

(declare-function triage-dispatch "triage-transient" ())
(declare-function triage-score-menu "triage-transient" ())
(declare-function triage-plot-menu "triage-transient" ())

(defvar-local triage-object--args nil
  "The triage arguments this view reads its payload with.")

(defvar-local triage-object--renderer nil
  "The function that inserts this view's sections from its payload.")

;;;; Loading a view

(defun triage-object--buffer (name mode)
  "Return the view buffer *triage: NAME* in MODE for the current project."
  (let ((state (triage--capture nil (or triage--root (triage--require-root))))
        (buffer (get-buffer-create (format "*triage: %s*" name))))
    (with-current-buffer buffer
      (unless (derived-mode-p mode)
        (funcall mode))
      (triage--restore state))
    buffer))

(defun triage-object--load ()
  "Read this view's payload again: one asynchronous call."
  (let ((buffer (current-buffer)))
    (if triage--loading
        (message "triage: still reading")
      (setq triage--loading t)
      (force-mode-line-update)
      (triage--call-json
       triage-object--args
       (lambda (payload) (triage-object--receive buffer payload nil))
       :errback (lambda (msg) (triage-object--receive buffer nil msg))))))

(defun triage-object--receive (buffer payload err)
  "Draw PAYLOAD, or the error message ERR, in the view BUFFER."
  (when (buffer-live-p buffer)
    (with-current-buffer buffer
      (setq triage--loading nil
            triage--load-error err)
      (when payload
        (setq triage--payload payload))
      (triage-object--render)
      (force-mode-line-update))))

(defun triage-object--render ()
  "Draw this view from its payload."
  (let ((payload triage--payload))
    (triage--redraw
     'triage-view
     (lambda ()
       (when triage--load-error
         (insert (propertize (format "Error: %s (see %s)\n\n" triage--load-error
                                     triage-errors-buffer)
                             'font-lock-face 'triage-danger)))
       (cond (payload (funcall triage-object--renderer payload))
             (triage--load-error nil)
             (t (insert (propertize "Loading…\n" 'font-lock-face 'triage-dim))))))))

(defun triage-object--show (name mode args renderer &optional context)
  "Open the view *triage: NAME* in MODE from the JSON of triage ARGS.
RENDERER inserts the sections from the payload.  CONTEXT is the
plist of `triage--context'.  Return the buffer."
  (let ((buffer (triage-object--buffer name mode)))
    (with-current-buffer buffer
      (setq triage-object--args args
            triage-object--renderer renderer
            triage--context context
            triage--refresh-function #'triage-object--load
            mode-line-process '(triage--loading " reading…"))
      (unless triage--payload
        (triage-object--render))
      (triage-object--load))
    (pop-to-buffer buffer)
    buffer))

;;;; Text helpers

(defun triage-object--model-type (type)
  "Return the class name at the end of the dotted model TYPE."
  (if (stringp type) (car (last (split-string type "\\."))) (triage--str type)))

(defun triage-object--insert-rows (rows columns)
  "Insert ROWS as aligned text with COLUMNS, a list of (KEY WIDTH)."
  (dolist (row rows)
    (insert "  "
            (mapconcat (lambda (col)
                         (triage--pad (triage--str (alist-get (car col) row)) (cadr col)))
                       columns " ")
            "\n")))

(defun triage-object--insert-alist (alist)
  "Insert each key and value of ALIST as a key line."
  (dolist (pair alist)
    (triage--key-line (symbol-name (car pair)) (cdr pair))))

;;;; Experiment view

(defvar triage-experiment-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "e") #'triage-experiment-edit)
    map)
  "Keymap for `triage-experiment-mode'.")

(define-derived-mode triage-experiment-mode triage-section-mode "Triage-Experiment"
  "Mode for the view of one Experiment.

\\{triage-experiment-mode-map}"
  :group 'triage
  (setq triage--visit-functions
        `((triage-run . ,(lambda (id _data) (triage-run-show id)))
          (triage-model-group . ,(lambda (id data)
                                   (triage-model-group-show (plist-get data :hash) id)))
          (triage-model . ,(lambda (id _data) (triage-model-show id))))))

;;;###autoload
(defun triage-experiment-show (target)
  "Show the Experiment of TARGET: identity, validation, plan, history.
TARGET is a config file, relative to the project root or absolute,
or an experiment_hash or a prefix of one."
  (interactive (list (triage--read-target :config "Experiment config: ")))
  (let* ((root (or triage--root (triage--require-root)))
         (file (and (file-exists-p (expand-file-name target root))
                    (file-relative-name (expand-file-name target root) root))))
    (triage-object--show
     (format "experiment %s" (if file (file-name-nondirectory file) (triage--short target)))
     #'triage-experiment-mode
     (list "experiment" "show" (or file target) "--json")
     #'triage-experiment--insert
     (if file (list :config file) (list :hash target)))))

(defun triage-experiment--insert (payload)
  "Insert the sections of the Experiment PAYLOAD."
  (let ((hash (alist-get 'experiment_hash payload))
        (path (alist-get 'path payload)))
    (setq triage--context (list :hash hash :config path))
    (triage-experiment--insert-identity payload)
    (triage-experiment--insert-validation (alist-get 'validation payload))
    (triage-experiment--insert-plan payload)
    (triage-experiment--insert-history payload hash)))

(defun triage-experiment--insert-identity (payload)
  "Insert the Identity section of PAYLOAD."
  (magit-insert-section (triage-identity)
    (magit-insert-heading
      (format "Experiment %s" (or (alist-get 'name payload)
                                  (triage--short (alist-get 'experiment_hash payload)))))
    (triage--key-line "hash" (alist-get 'experiment_hash payload) 'triage-dim)
    (triage--key-line "name" (alist-get 'name payload))
    (triage--key-line "problem_type" (alist-get 'problem_type payload))
    (triage--key-line "task_framing" (alist-get 'task_framing payload))
    (triage--key-line "source" (if (equal (alist-get 'source payload) "file")
                                   (alist-get 'path payload)
                                 "stored config (e shows it)"))
    (insert "\n")))

(defun triage-experiment--insert-validation (validation)
  "Insert the Validation section for VALIDATION, folded when clean."
  (let* ((errors (alist-get 'errors validation))
         (warnings (alist-get 'warnings validation))
         (clean (and (null errors) (null warnings))))
    (magit-insert-section (triage-validation nil clean)
      (magit-insert-heading
        (concat "Validation: "
                (cond (errors (propertize (format "%d errors" (length errors))
                                          'font-lock-face 'triage-danger))
                      (warnings (propertize (format "valid, %d warnings" (length warnings))
                                            'font-lock-face 'triage-warning))
                      (t (propertize "valid" 'font-lock-face 'triage-ok)))))
      (dolist (e errors)
        (insert "  " (propertize (triage--str e) 'font-lock-face 'triage-danger) "\n"))
      (dolist (w warnings)
        (insert "  " (propertize (triage--str w) 'font-lock-face 'triage-warning) "\n"))
      (when clean (insert "  no errors, no warnings\n"))
      (insert "\n"))))

(defun triage-experiment--insert-plan (payload)
  "Insert the Plan section of PAYLOAD."
  (let ((plan (alist-get 'plan payload))
        (plan-error (alist-get 'plan_error payload)))
    (magit-insert-section (triage-plan)
      (magit-insert-heading "Plan")
      (if plan-error
          (insert "  " (propertize (triage--str plan-error) 'font-lock-face 'triage-danger) "\n")
        (triage--key-line "splits" (alist-get 'n_splits plan))
        (triage--key-line "test matrices" (alist-get 'n_test_matrices plan))
        (triage--key-line "as_of_dates" (alist-get 'n_as_of_dates plan))
        (triage--key-line "label timespans"
                          (mapconcat #'triage--str (alist-get 'label_timespans plan) ", "))
        (triage--key-line "matrices to build" (alist-get 'n_matrices plan))
        (triage--key-line "grid size" (alist-get 'grid_size plan))
        (triage--key-line "model groups" (alist-get 'n_model_groups plan))
        (triage--key-line "feature-group runs" (alist-get 'n_runs plan))
        (triage--key-line "models to train" (alist-get 'n_models plan) 'bold)
        (triage--key-line "feature columns" (alist-get 'n_feature_columns plan))
        (triage--key-line "truncated columns" (alist-get 'n_truncated_columns plan))
        (let ((subsets (alist-get 'subsets plan)))
          (magit-insert-section (triage-subsets nil (> (length subsets) 5))
            (magit-insert-heading (format "  Subsets (%d)" (length subsets)))
            (dolist (subset subsets)
              (insert "    " (triage--pad (triage--str (alist-get 'label subset)) 28)
                      " " (format "%5s columns  " (triage--str (alist-get 'n_columns subset)))
                      (mapconcat #'triage--str (alist-get 'group_names subset) ", ")
                      "\n"))))
        (dolist (issue (alist-get 'baseline_issues plan))
          (insert "  " (propertize (concat "baseline: " (triage--str issue))
                                   'font-lock-face 'triage-warning) "\n"))
        (dolist (warning (alist-get 'warnings plan))
          (insert "  " (propertize (triage--str warning) 'font-lock-face 'triage-warning) "\n"))
        (when-let* ((feature-error (alist-get 'feature_error plan)))
          (insert "  " (propertize (concat "features: " (triage--str feature-error))
                                   'font-lock-face 'triage-danger) "\n")))
      (insert "\n"))))

(defun triage-experiment--insert-history (payload hash)
  "Insert the History section of PAYLOAD for the Experiment HASH."
  (let ((history (alist-get 'history payload))
        (database (alist-get 'database payload)))
    (magit-insert-section (triage-history)
      (magit-insert-heading "History")
      (cond
       ((and (null history) (not (eq (alist-get 'connected database) t)))
        (insert "  " (propertize (concat "database unreachable: "
                                         (triage--str (alist-get 'detail database)))
                                 'font-lock-face 'triage-danger)
                "\n\n"))
       ((null history)
        (insert "  " (propertize "not run yet" 'font-lock-face 'triage-dim) "\n\n"))
       (t
        (let ((summary (alist-get 'summary history)))
          (triage--key-line "runs" (alist-get 'n_runs summary))
          (triage--key-line "last status" (alist-get 'last_status summary))
          (triage--key-line "last started" (triage--time (alist-get 'last_started_at summary)))
          (triage--key-line "created" (triage--time (alist-get 'created_at summary)))
          (triage--key-line "author" (alist-get 'author summary))
          (triage--key-line "cohort size" (alist-get 'cohort_size summary))
          (triage--key-line "base rate" (alist-get 'base_rate summary))
          (insert "\n"))
        (triage-experiment--insert-runs (alist-get 'runs history))
        (triage-experiment--insert-model-groups history hash)
        (triage-experiment--insert-leaderboard (alist-get 'leaderboard history) hash))))))

(defun triage-experiment--insert-runs (rows)
  "Insert the run ROWS of an Experiment."
  (magit-insert-section (triage-runs)
    (magit-insert-heading (format "  Runs (%d)" (length rows)))
    (dolist (run rows)
      (let ((id (alist-get 'run_id run)))
        (triage--remember 'triage-run id (list :run id :row run))
        (magit-insert-section (triage-run id)
          (insert "    " (propertize (triage--pad id 9) 'font-lock-face 'triage-dim)
                  " " (triage--pad (triage--str (alist-get 'state run)) 10)
                  " " (triage--pad (triage--str (alist-get 'detail run)) 14)
                  " " (triage--time (alist-get 'started_at run)) "\n"))))
    (insert "\n")))

(defun triage-experiment--insert-model-groups (history hash)
  "Insert the model groups of HISTORY for the Experiment HASH."
  (let ((groups (alist-get 'model_groups history))
        (metric (alist-get 'model_group_metric history)))
    (magit-insert-section (triage-model-groups)
      (magit-insert-heading
        (format "  Model groups (%d) on %s%s" (length groups)
                (triage--str (alist-get 'metric metric))
                (let ((p (alist-get 'parameter metric)))
                  (if (and p (not (string-empty-p p))) (concat " " p) ""))))
      (insert (propertize (format "    %-6s %-28s %6s  %-17s  %s\n"
                                  "group" "algorithm" "models" "avg ± σ" "max regret")
                          'font-lock-face 'triage-dim))
      (dolist (group groups)
        (let ((id (alist-get 'model_group_id group)))
          (triage--remember 'triage-model-group id (list :group id :hash hash :row group))
          (magit-insert-section (triage-model-group id)
            (insert (format "    %-6s %-28s %6s  %-17s  %s\n"
                            id
                            (triage--pad (triage-object--model-type (alist-get 'model_type group)) 28)
                            (triage--str (alist-get 'n_models group))
                            (format "%s ± %s" (triage--str (alist-get 'avg_value group))
                                    (triage--str (alist-get 'stddev_value group)))
                            (triage--str (alist-get 'max_regret group)))))))
      (insert "\n"))))

(defun triage-experiment--insert-leaderboard (rows hash)
  "Insert the leaderboard ROWS of the Experiment HASH."
  (magit-insert-section (triage-leaderboard)
    (magit-insert-heading (format "  Leaderboard (top %d)" (length rows)))
    (insert (propertize (format "    %-6s %-6s %-10s %-24s %s\n"
                                "group" "model" "as_of_date" "metric" "value")
                        'font-lock-face 'triage-dim))
    (dolist (row rows)
      (let ((model (alist-get 'model_id row)))
        (triage--remember 'triage-model model
                          (list :model model :group (alist-get 'model_group_id row)
                                :hash hash :row row))
        (magit-insert-section (triage-model model)
          (insert (format "    %-6s %-6s %-10s %-24s %s\n"
                          (alist-get 'model_group_id row) model
                          (triage--str (alist-get 'as_of_date row))
                          (triage--pad (concat (triage--str (alist-get 'metric row))
                                               (let ((p (alist-get 'parameter row)))
                                                 (if (and p (not (string-empty-p p)))
                                                     (concat "@" p) "")))
                                       24)
                          (triage--str (alist-get 'value row)))))))
    (insert "\n")))

(defun triage-experiment-edit ()
  "Visit this Experiment's config file, or show its stored config.
A view opened from a hash shows the config the database stored, as
YAML the CLI formatted, in a read-only buffer."
  (interactive)
  (let ((payload triage--payload))
    (cond
     ((null payload) (user-error "The Experiment has not loaded yet"))
     ((equal (alist-get 'source payload) "file")
      (find-file (expand-file-name (alist-get 'path payload) triage--root)))
     ((alist-get 'config_yaml payload)
      (triage-experiment--show-stored-config payload))
     (t (user-error "The CLI sent no stored config for this Experiment")))))

(defun triage-experiment--show-stored-config (payload)
  "Show the stored config of the Experiment PAYLOAD as read-only YAML."
  (let ((buffer (get-buffer-create
                 (format "*triage: config %s*"
                         (triage--short (alist-get 'experiment_hash payload))))))
    (with-current-buffer buffer
      (let ((inhibit-read-only t))
        (erase-buffer)
        (insert (alist-get 'config_yaml payload))
        (goto-char (point-min)))
      (cond ((fboundp 'yaml-mode) (funcall 'yaml-mode))
            ((and (fboundp 'yaml-ts-mode) (fboundp 'treesit-ready-p)
                  (treesit-ready-p 'yaml t))
             (funcall 'yaml-ts-mode)))
      (setq buffer-read-only t)
      (set-buffer-modified-p nil))
    (pop-to-buffer buffer)))

;;;; Run view

(defvar triage-run-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "t") #'triage-run-tail)
    map)
  "Keymap for `triage-run-mode'.")

(define-derived-mode triage-run-mode triage-section-mode "Triage-Run"
  "Mode for the view of one Run.

\\{triage-run-mode-map}"
  :group 'triage)

;;;###autoload
(defun triage-run-show (run-id)
  "Show the Run RUN-ID: its row, its stages and its meta."
  (interactive (list (triage--read-target :run "Run: ")))
  (triage-object--show
   (format "run %s" (substring run-id 0 (min 8 (length run-id))))
   #'triage-run-mode
   (list "runs" "show" run-id "--json")
   #'triage-run--insert
   (list :run run-id)))

(defun triage-run--insert (payload)
  "Insert the sections of the Run PAYLOAD."
  (let ((run (alist-get 'run payload))
        (meta (alist-get 'meta payload)))
    (setq triage--context (list :run (alist-get 'run_id run)
                                :hash (alist-get 'experiment meta)))
    (magit-insert-section (triage-run-row)
      (magit-insert-heading (format "Run %s" (triage--str (alist-get 'name run))))
      (triage--key-line "run_id" (alist-get 'run_id run) 'triage-dim)
      (triage--key-line "state" (alist-get 'state run)
                        (pcase (alist-get 'state run)
                          ("succeeded" 'triage-ok) ("failed" 'triage-danger) (_ nil)))
      (triage--key-line "started" (triage--time (alist-get 'started_at run)))
      (triage--key-line "finished" (triage--time (alist-get 'finished_at run)))
      (triage--key-line "detail" (alist-get 'detail run))
      (insert "\n"))
    (magit-insert-section (triage-stages)
      (magit-insert-heading "Stages")
      (dolist (stage (alist-get 'stages payload))
        (let ((done (alist-get 'done stage))
              (total (alist-get 'total stage)))
          (insert "  " (triage--pad (triage--str (alist-get 'name stage)) 14)
                  (format " %6s / %-6s " (triage--str done) (triage--str total))
                  (triage-run--bar done total)
                  " " (triage--str (alist-get 'note stage)) "\n")))
      (insert "\n"))
    (magit-insert-section (triage-meta)
      (magit-insert-heading "Meta")
      (triage-object--insert-alist meta)
      (insert "\n"))))

(defun triage-run--bar (done total)
  "Return a 20-cell progress bar for DONE of TOTAL."
  (if (and (numberp done) (numberp total) (> total 0))
      (let ((filled (min 20 (/ (* 20 done) total))))
        (concat (propertize (make-string filled ?█) 'font-lock-face 'triage-ok)
                (propertize (make-string (- 20 filled) ?·) 'font-lock-face 'triage-dim)))
    (make-string 20 ?\s)))

(defun triage-run-tail ()
  "Follow the events of this Run in a process buffer."
  (interactive)
  (let ((id (triage--read-target :run "Run: ")))
    (triage--run-in-buffer (format "runs tail %s" (substring id 0 (min 8 (length id))))
                           (list "runs" "tail" id))))

;;;; Model view

(define-derived-mode triage-model-mode triage-section-mode "Triage-Model"
  "Mode for the view of one model."
  :group 'triage)

;;;###autoload
(defun triage-model-show (model-id)
  "Show the model MODEL-ID: its card, windowed metrics and diagnostics."
  (interactive (list (triage--read-target :model "Model id: ")))
  (let ((id (if (numberp model-id) (number-to-string model-id) model-id)))
    (triage-object--show (format "model %s" id) #'triage-model-mode
                         (list "model" "show" id "--json")
                         #'triage-model--insert
                         (list :model id))))

(defun triage-model--insert (payload)
  "Insert the sections of the model PAYLOAD."
  (let ((card (alist-get 'card payload)))
    (setq triage--context (list :model (alist-get 'model_id card)
                                :group (alist-get 'model_group_id card)))
    (magit-insert-section (triage-card)
      (magit-insert-heading (format "Model %s" (alist-get 'model_id card)))
      (triage-object--insert-alist card)
      (insert "\n"))
    (dolist (part '((windowed "Windowed metrics" metric parameter n_as_of_dates
                              value_mean value_min value_max)
                    (importances "Feature importances")
                    (calibration "Calibration")
                    (crosstabs "Crosstabs")))
      (let* ((rows (alist-get (car part) payload))
             (keys (or (cddr part) (mapcar #'car (car rows)))))
        (magit-insert-section ((eval (intern (format "triage-%s" (car part)))) nil
                               (null rows))
          (magit-insert-heading (format "%s (%d)" (cadr part) (length rows)))
          (when rows
            (insert (propertize (concat "  " (mapconcat (lambda (k) (triage--pad (symbol-name k) 14))
                                                        keys " ")
                                        "\n")
                                'font-lock-face 'triage-dim))
            (triage-object--insert-rows (seq-take rows 50)
                                        (mapcar (lambda (k) (list k 14)) keys)))
          (insert "\n"))))))

;;;; Tables: model groups, relations, queries

(defvar-local triage-table--on-ret nil
  "The function RET calls with the row at point, or nil.")

(defvar-local triage-table--rows nil
  "The rows this table view shows.")

(defvar triage-table-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "g") #'triage-refresh)
    (define-key map (kbd "RET") #'triage-table-visit)
    (define-key map (kbd "?") #'triage-dispatch)
    (define-key map (kbd "f") #'triage-score-menu)
    (define-key map (kbd "p") #'triage-plot-menu)
    (define-key map (kbd ":") #'triage-query)
    (define-key map (kbd "q") #'quit-window)
    map)
  "Keymap for `triage-table-mode'.")

(define-derived-mode triage-table-mode tabulated-list-mode "Triage-Table"
  "Mode for rows the triage CLI printed as JSON.

\\{triage-table-mode-map}"
  :group 'triage
  ;; The column names go in the buffer; the header line says what the rows are.
  (setq tabulated-list-use-header-line nil)
  (setq mode-line-process '(triage--loading " reading…")))

(defun triage-table--columns (rows)
  "Return a `tabulated-list-format' vector for ROWS."
  (let ((keys (mapcar #'car (car rows))))
    (vconcat
     (mapcar (lambda (key)
               (let* ((values (mapcar (lambda (r) (alist-get key r)) rows))
                      (numeric (seq-every-p (lambda (v) (or (null v) (numberp v))) values))
                      (width (min 40 (apply #'max (length (symbol-name key))
                                            (mapcar (lambda (v) (length (triage--str v)))
                                                    (seq-take values 200))))))
                 (list (symbol-name key) width
                       (if numeric
                           (lambda (a b)
                             (< (or (alist-get key (car a)) most-negative-fixnum)
                                (or (alist-get key (car b)) most-negative-fixnum)))
                         t)
                       :right-align numeric)))
             keys))))

(defun triage-table--fill (rows)
  "Show ROWS in the current table view."
  (setq triage-table--rows rows
        tabulated-list-format (triage-table--columns rows)
        tabulated-list-entries
        (mapcar (lambda (row)
                  (list row (vconcat (mapcar (lambda (pair) (triage--str (cdr pair))) row))))
                rows))
  (tabulated-list-init-header)
  (tabulated-list-print t))

(defun triage-table--show (name args &rest keys)
  "Show the rows of triage ARGS in the table view *triage: NAME*.
KEYS are :rows-function (from the payload to the rows, default
identity), :header (a function from the payload to the header
line), :on-ret (called with the row at RET) and :row-context (a
function from a row to the plist the menus read).  Return the
buffer."
  (let ((buffer (triage-object--buffer name #'triage-table-mode)))
    (with-current-buffer buffer
      (setq triage-table--on-ret (plist-get keys :on-ret)
            triage--row-context-function (plist-get keys :row-context)
            triage--refresh-function
            (lambda () (triage-table--load buffer args
                                           (or (plist-get keys :rows-function) #'identity)
                                           (plist-get keys :header))))
      (setq header-line-format (mapconcat #'identity (cons "triage" args) " "))
      (funcall triage--refresh-function))
    (pop-to-buffer buffer)
    buffer))

(defun triage-table--load (buffer args rows-function header)
  "Read triage ARGS into the table BUFFER through ROWS-FUNCTION.
HEADER, when non-nil, makes the header line from the payload."
  (with-current-buffer buffer
    (setq triage--loading t)
    (force-mode-line-update))
  (triage--call-json
   args
   (lambda (payload)
     (when (buffer-live-p buffer)
       (with-current-buffer buffer
         (setq triage--loading nil
               triage--load-error nil
               triage--payload payload)
         (when header (setq header-line-format (funcall header payload)))
         (triage-table--fill (funcall rows-function payload))
         (force-mode-line-update))))
   :errback
   (lambda (msg)
     (when (buffer-live-p buffer)
       (with-current-buffer buffer
         (setq triage--loading nil
               triage--load-error msg
               header-line-format (propertize msg 'face 'triage-danger))
         (triage-table--fill nil)
         (force-mode-line-update)))
     (message "triage: %s" msg))))

(defun triage-table-visit ()
  "Open the row at point."
  (interactive)
  (let ((row (tabulated-list-get-id)))
    (cond ((null row) (user-error "No row at point"))
          (triage-table--on-ret (funcall triage-table--on-ret row))
          (t (user-error "Rows here open nothing")))))

;;;###autoload
(defun triage-model-group-show (hash group)
  "Show the member models of model GROUP of the Experiment HASH."
  (interactive (list (triage--read-target :hash "Experiment: ")
                     (triage--read-target :group "Model group: ")))
  (let ((group (if (numberp group) (number-to-string group) group)))
    (triage-table--show
     (format "models %s group %s" (triage--short hash) group)
     (list "models" hash "--group" group "--json")
     :rows-function (lambda (payload) (alist-get 'models payload))
     :header (lambda (payload)
               (format "Model group %s of %s · group average %s" group (triage--short hash)
                       (triage--str (alist-get 'group_avg payload))))
     :on-ret (lambda (row) (triage-model-show (alist-get 'model_id row)))
     :row-context (lambda (row) (list :model (alist-get 'model_id row)
                                      :group group :hash hash)))))

(defun triage--quote-ident (name)
  "Return NAME as a double-quoted SQL identifier."
  (concat "\"" (string-replace "\"" "\"\"" name) "\""))

;;;###autoload
(defun triage-table-show (name)
  "Show the first `triage-table-limit' rows of the relation triage.NAME."
  (interactive (list (read-string "Relation in schema triage: ")))
  (triage-table--show
   (format "table %s" name)
   (list "query" "--json"
         (format "select * from triage.%s limit %d" (triage--quote-ident name) triage-table-limit))))

(defvar triage-query-history nil
  "SQL statements read by `triage-query'.")

;;;###autoload
(defun triage-query (sql)
  "Show the rows of the read-only SQL statement, read with triage query."
  (interactive (list (read-string "SQL (read-only): " nil 'triage-query-history)))
  (triage-table--show "query" (list "query" "--json" sql)))

;;;; Any JSON object

(define-derived-mode triage-json-mode triage-section-mode "Triage-JSON"
  "Mode for a JSON object a triage verb printed, such as audition."
  :group 'triage
  (setq triage--visit-functions
        `((triage-model-group . ,(lambda (id data)
                                   (triage-model-group-show (plist-get data :hash) id)))
          (triage-model . ,(lambda (id _data) (triage-model-show id))))))

(defun triage-json-show (name args)
  "Show the JSON object that triage ARGS prints in the view *triage: NAME*."
  (triage-object--show name #'triage-json-mode args #'triage-object--insert-json))

(defun triage-object--insert-json (payload)
  "Insert PAYLOAD: scalars as key lines, objects nested, lists of objects as rows.
A row with a model_group_id or model_id opens it on RET."
  (let ((hash (alist-get 'experiment_hash payload)))
    (when hash (setq triage--context (list :hash hash)))
    (dolist (pair payload)
      (let ((key (symbol-name (car pair)))
            (value (cdr pair)))
        (cond
         ((and (consp value) (consp (car value)) (symbolp (caar value)))
          (magit-insert-section (triage-json-object key)
            (magit-insert-heading key)
            (dolist (inner value)
              (triage--key-line (concat "  " (symbol-name (car inner))) (cdr inner)))
            (insert "\n")))
         ((and (consp value) (consp (car value)))
          (let ((keys (mapcar #'car (car value))))
            (magit-insert-section (triage-json-rows key)
              (magit-insert-heading (format "%s (%d)" key (length value)))
              (insert (propertize (concat "  " (mapconcat (lambda (k) (triage--pad (symbol-name k) 14))
                                                          keys " ")
                                          "\n")
                                  'font-lock-face 'triage-dim))
              (seq-do-indexed
               (lambda (row i)
                 (let* ((model (alist-get 'model_id row))
                        (group (alist-get 'model_group_id row))
                        (type (cond (model 'triage-model) (group 'triage-model-group)
                                    (t 'triage-json-row)))
                        (value (or model group (format "%s-%d" key i))))
                   (triage--remember type value (list :model model :group group :hash hash :row row))
                   (magit-insert-section ((eval type) value)
                     (triage-object--insert-rows
                      (list row) (mapcar (lambda (k) (list k 14)) keys)))))
               value)
              (insert "\n"))))
         (t (triage--key-line key value)))))))

;;;; Source view

(defun triage-source-show (source)
  "Show the SOURCE row of the status payload, without a call."
  (let ((buffer (get-buffer-create
                 (format "*triage: source %s*" (triage--str (alist-get 'source_name source))))))
    (with-current-buffer buffer
      (let ((inhibit-read-only t))
        (erase-buffer)
        (triage-object--insert-alist source)
        (goto-char (point-min)))
      (special-mode))
    (pop-to-buffer buffer)))

(provide 'triage-object)

;;; triage-object.el ends here
