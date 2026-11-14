;;; triage-postmodel.el --- The postmodel menu and its result views  -*- lexical-binding: t; -*-

;; Copyright (c) 2026 Adolfo De Unánue and triage-pg contributors
;; SPDX-License-Identifier: MIT

;; This file is not part of GNU Emacs.

;;; Commentary:

;; `triage-postmodel-menu' runs the triage postmodel diagnostics on the
;; model, model group or Experiment at point.  Each suffix passes only
;; the flags its subcommand takes.  An entry is greyed out when the
;; problem type at point is one its command refuses, by the table
;; `triage--refuses-p' reads from triage postmodel kinds --json, so the
;; menu does not offer a command that would exit 2.  The error tree
;; offers the kinds the type at point reads, and asks for --horizon
;; when the type needs one.
;;
;; A subcommand that prints JSON opens a view drawn from it, with one
;; table per part of the result.  Those without --json (error-tree,
;; crosstabs and compare) print their own tables in a process buffer.

;;; Code:

(require 'triage)
(require 'transient)
(require 'triage-object)
(require 'triage-transient)

(defconst triage-postmodel-commands
  '(("residuals" :model ("--by" "--top" "--quantiles"))
    ("structure" :model ("--min-rows"))
    ("survival" :model ("--horizon" "--times"))
    ("near-miss" :model ("--parameter" "--width" "--date"))
    ("prototypes" :model ("--parameter" "--n" "--date"))
    ("error-tree" :model ("--parameter" "--kind" "--depth" "--min-leaf" "--baseline"
                          "--horizon"))
    ("crosstabs" :model ("--parameter"))
    ("compare" :models ("--parameter" "--sweep"))
    ("baseline" :group ("--baseline" "--cut" "--experiment"))
    ("importance" :group ("--top"))
    ("compare-rows" :hash ("--groups"))
    ("sensitivity" :hash ("--param" "--metric" "--parameter" "--model-type"
                          "--common-rows")))
  "Each triage postmodel subcommand the menu runs: its target and its flags.
The flags are those the menu passes through; a suffix may add its
own, such as --against=near-miss.  The ERT suite checks every name
and every flag a suffix passes against the CLI's command tree.")

;;;; Predicates

(triage-transient--define-gate triage-postmodel-residuals--inapt-p
                               (:model) "postmodel residuals")
(triage-transient--define-gate triage-postmodel-structure--inapt-p
                               (:model) "postmodel structure")
(triage-transient--define-gate triage-postmodel-survival--inapt-p
                               (:model) "postmodel survival")
(triage-transient--define-gate triage-postmodel-near-miss--inapt-p
                               (:model) "postmodel near-miss")
(triage-transient--define-gate triage-postmodel-prototypes--inapt-p
                               (:model) "postmodel prototypes")
(triage-transient--define-gate triage-postmodel-error-tree--inapt-p
                               (:model) "postmodel error-tree")
(triage-transient--define-gate triage-postmodel-crosstabs-abs-error--inapt-p
                               (:model) "postmodel crosstabs --by abs-error")
(triage-transient--define-gate triage-postmodel-crosstabs-near-miss--inapt-p
                               (:model) "postmodel crosstabs --against near-miss")
(triage-transient--define-gate triage-postmodel-compare--inapt-p
                               (:model) "postmodel compare")
(triage-transient--define-gate triage-postmodel-baseline--inapt-p
                               (:group) "postmodel baseline")
(triage-transient--define-gate triage-postmodel-importance--inapt-p
                               (:group) "postmodel importance")
(triage-transient--define-gate triage-postmodel-compare-rows--inapt-p
                               (:hash) "postmodel compare-rows")
(triage-transient--define-gate triage-postmodel-sensitivity--inapt-p
                               (:hash) "postmodel sensitivity")

;;;; Readers

(defun triage-postmodel--kinds-at-point ()
  "Return the error-tree kinds of the problem type at point, or nil.
With no type at point, every kind the table knows; with no table, nil."
  (with-current-buffer (triage-transient--buffer)
    (or (alist-get 'kinds (triage--error-tree-rule (triage--at-point :type)))
        (seq-uniq (seq-mapcat (lambda (rule) (alist-get 'kinds (cdr rule)))
                              (alist-get 'error_tree (triage--kinds)))))))

(defun triage-postmodel--read-kind (prompt initial-input history)
  "Read an error-tree kind with PROMPT, INITIAL-INPUT and HISTORY.
The candidates are the kinds the problem type at point reads."
  (completing-read prompt (triage-postmodel--kinds-at-point) nil nil initial-input history))

(defun triage-postmodel--ensure-flag (args flag read)
  "Return ARGS with FLAG=VALUE added when they lack FLAG.
VALUE comes from calling READ."
  (if (triage-transient--has-flag-p args flag)
      args
    (append args (list (concat flag "=" (funcall read))))))

(defun triage-postmodel--read-horizon ()
  "Read the horizon of a survival diagnostic, or signal that there is none."
  (let ((horizon (string-trim (read-string "Horizon (in the units of the label's duration): "))))
    (if (string-empty-p horizon)
        (user-error "A survival model's diagnostics are read at a horizon: give --horizon")
      horizon)))

;;;; The menu

;;;###autoload (autoload 'triage-postmodel-menu "triage-postmodel" nil t)
(transient-define-prefix triage-postmodel-menu ()
  "Postmodel diagnostics of the model, model group or Experiment at point.
An entry is greyed out when nothing it runs on is at hand, or when
the problem type at point is one its command refuses."
  ["Arguments"
   [("-p" "Cut, or the metric's parameter" "--parameter=")
    ("-d" "Test date (YYYY-MM-DD)" "--date=")
    ("-w" "Near-miss band width (a cut)" "--width=")
    ("-n" "Prototypes per class" "--n=" :reader transient-read-number-N+)
    ("-k" "Error-tree kind" "--kind=" :reader triage-postmodel--read-kind)
    ("-D" "Error-tree depth" "--depth=" :reader transient-read-number-N+)
    ("-l" "Error-tree rows per leaf" "--min-leaf=" :reader transient-read-number-N+)
    ("-H" "Horizon (survival)" "--horizon=")
    ("-T" "Times of AUC(t) and Brier(t) (T1,T2)" "--times=")]
   [("-b" "Residuals by (date, band, sbc, cold-start, subset:NAME)" "--by="
     :reader triage-transient--read-cut)
    ("-t" "Top (largest errors, features compared)" "--top=" :reader transient-read-number-N+)
    ("-q" "Quantile coverage" "--quantiles")
    ("-m" "Leaves on fewer training rows than" "--min-rows=" :reader transient-read-number-N+)
    ("-B" "Baseline model group" "--baseline=" :reader transient-read-number-N+)
    ("-c" "Baseline cut (band, sbc, cold-start, all, subset:NAME)" "--cut="
     :reader triage-transient--read-cut)
    ("-e" "Experiment of the group (a hash prefix)" "--experiment=")
    ("-g" "Groups to compare (G1,G2)" "--groups=")
    ("-s" "Sweep these cuts (compare)" "--sweep=")]
   [("-P" "Hyperparameter (sensitivity)" "--param=")
    ("-M" "Metric (sensitivity)" "--metric=")
    ("-y" "Model class (sensitivity)" "--model-type=")
    ("-C" "On the rows every group scored" "--common-rows")]]
  [["Model"
    ("r" "Residuals" triage-postmodel-residuals :inapt-if triage-postmodel-residuals--inapt-p)
    ("s" "Structure" triage-postmodel-structure :inapt-if triage-postmodel-structure--inapt-p)
    ("S" "Survival diagnostics" triage-postmodel-survival
     :inapt-if triage-postmodel-survival--inapt-p)
    ("n" "Near miss" triage-postmodel-near-miss :inapt-if triage-postmodel-near-miss--inapt-p)
    ("o" "Prototypes" triage-postmodel-prototypes :inapt-if triage-postmodel-prototypes--inapt-p)
    ("e" "Error tree" triage-postmodel-error-tree :inapt-if triage-postmodel-error-tree--inapt-p)
    ("x" "Crosstabs of the largest errors" triage-postmodel-crosstabs-abs-error
     :inapt-if triage-postmodel-crosstabs-abs-error--inapt-p)
    ("X" "Crosstabs against the near miss" triage-postmodel-crosstabs-near-miss
     :inapt-if triage-postmodel-crosstabs-near-miss--inapt-p)
    ("c" "Compare with another model" triage-postmodel-compare
     :inapt-if triage-postmodel-compare--inapt-p)]
   ["Model group"
    ("b" "Against the baseline" triage-postmodel-baseline
     :inapt-if triage-postmodel-baseline--inapt-p)
    ("i" "Importance by family" triage-postmodel-importance
     :inapt-if triage-postmodel-importance--inapt-p)]
   ["Experiment"
    ("w" "Own rows and common rows" triage-postmodel-compare-rows
     :inapt-if triage-postmodel-compare-rows--inapt-p)
    ("v" "Sensitivity to a hyperparameter" triage-postmodel-sensitivity
     :inapt-if triage-postmodel-sensitivity--inapt-p)]])

;;;; Running the subcommands

(define-derived-mode triage-postmodel-mode triage-section-mode "Triage-Postmodel"
  "Mode for the result of a triage postmodel diagnostic."
  :group 'triage
  (setq triage--visit-functions
        `((triage-model . ,(lambda (id _data) (triage-model-show id)))
          (triage-model-group . ,(lambda (id data)
                                   (triage-model-group-show (plist-get data :hash) id))))))

(defun triage-postmodel--id (value)
  "Return the id VALUE as a string."
  (if (numberp value) (number-to-string value) value))

(defun triage-postmodel--args (name args)
  "Return the elements of ARGS that the menu passes to triage postmodel NAME."
  (triage-transient--only args (nth 2 (assoc name triage-postmodel-commands))))

(defun triage-postmodel--context (kind value)
  "Return the context of a view about the KIND VALUE, such as :model 3.
It carries the Experiment and the problem type at point, which hold
for every item the menu could have prompted for here."
  (list kind value :hash (triage--at-point :hash) :type (triage--at-point :type)))

(defun triage-postmodel--show (name args renderer context &optional accept)
  "Open *triage: NAME* from the JSON of triage ARGS, drawn by RENDERER.
CONTEXT is the view's `triage--context'.  ACCEPT lists the non-zero
exit codes whose JSON is still a result."
  (triage-object--show name #'triage-postmodel-mode args renderer context accept))

(defun triage-postmodel-residuals (model args)
  "Show where MODEL's errors are, by date, history segment and entity.
ARGS are the menu's flags: --by keeps one table."
  (interactive (list (triage--read-target :model "Model: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((model (triage-postmodel--id model)))
    (triage-postmodel--show (format "residuals %s" model)
                            (append (list "postmodel" "residuals" model "--json")
                                    (triage-postmodel--args "residuals" args))
                            #'triage-postmodel--insert-residuals
                            (triage-postmodel--context :model model))))

(defun triage-postmodel-structure (model args)
  "Show the structure of MODEL's fitted estimator, with the flags ARGS.
The view draws the result when the CLI found nothing to check too
\(its exit 1), with the reason."
  (interactive (list (triage--read-target :model "Model: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((model (triage-postmodel--id model)))
    (triage-postmodel--show (format "structure %s" model)
                            (append (list "postmodel" "structure" model "--json")
                                    (triage-postmodel--args "structure" args))
                            #'triage-postmodel--insert-structure
                            (triage-postmodel--context :model model)
                            '(1))))

(defun triage-postmodel-survival (model args)
  "Compute and show MODEL's survival diagnostics, with the flags ARGS.
The horizon is asked for when ARGS have no --horizon."
  (interactive (list (triage--read-target :model "Model: ")
                     (transient-args 'triage-postmodel-menu)))
  (let* ((model (triage-postmodel--id model))
         (args (triage-postmodel--ensure-flag (triage-postmodel--args "survival" args)
                                              "--horizon" #'triage-postmodel--read-horizon)))
    (triage-postmodel--show (format "survival %s" model)
                            (append (list "postmodel" "survival" model "--json") args)
                            #'triage-postmodel--insert-survival
                            (triage-postmodel--context :model model))))

(defun triage-postmodel-near-miss (model args)
  "Show the near-miss bands of MODEL on each scored date, with the flags ARGS."
  (interactive (list (triage--read-target :model "Model: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((model (triage-postmodel--id model))
        (context (triage-postmodel--context :model nil)))
    (triage-table--show (format "near-miss %s" model)
                        (append (list "postmodel" "near-miss" model "--json")
                                (triage-postmodel--args "near-miss" args))
                        :row-context (lambda (_row) (plist-put (copy-sequence context)
                                                               :model model)))))

(defun triage-postmodel-prototypes (model args)
  "Compute and show the prototypes of each class at MODEL's cut.
ARGS are the menu's flags."
  (interactive (list (triage--read-target :model "Model: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((model (triage-postmodel--id model)))
    (triage-postmodel--show (format "prototypes %s" model)
                            (append (list "postmodel" "prototypes" model "--json")
                                    (triage-postmodel--args "prototypes" args))
                            #'triage-postmodel--insert-prototypes
                            (triage-postmodel--context :model model))))

(defun triage-postmodel-error-tree (model args)
  "Fit the error trees of MODEL with the flags ARGS, in a process buffer.
The kinds offered are those the problem type at point reads.  A type
that needs --horizon (survival) is asked for one; for the others it
is left out."
  (interactive (list (triage--read-target :model "Model: ")
                     (transient-args 'triage-postmodel-menu)))
  (let* ((model (triage-postmodel--id model))
         (type (triage--at-point :type))
         (rule (triage--error-tree-rule type))
         (kinds (alist-get 'kinds rule))
         (args (triage-postmodel--args "error-tree" args))
         (kind (triage-transient--flag-value args "--kind"))
         (args (seq-remove (lambda (a) (string-prefix-p "--kind" a)) args)))
    (when (and kind kinds (not (member kind kinds)))
      (user-error "Model %s is %s: its error trees take --kind %s"
                  model type (string-join kinds ", ")))
    (when (and (null kind) kinds)
      (setq kind (completing-read "Error-tree kind: " kinds nil t nil nil (car kinds))))
    (pcase (alist-get 'horizon rule)
      ("required"
       (setq args (triage-postmodel--ensure-flag args "--horizon"
                                                 #'triage-postmodel--read-horizon)))
      ("refused"
       (setq args (seq-remove (lambda (a) (string-prefix-p "--horizon" a)) args))))
    (triage--run-in-buffer (format "error-tree %s" model)
                           (append (list "postmodel" "error-tree" model)
                                   (and kind (list (concat "--kind=" kind)))
                                   args))))

(defun triage-postmodel-crosstabs-abs-error (model args)
  "Compare MODEL's largest errors with the rest of its rows.
It runs in a process buffer.  The cut -p of ARGS sets how many rows
count as the largest errors; without it the CLI takes 10_pct."
  (interactive (list (triage--read-target :model "Model: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((model (triage-postmodel--id model))
        (cut (triage-transient--flag-value args "--parameter")))
    (triage--run-in-buffer (format "crosstabs %s" model)
                           (list "postmodel" "crosstabs" model
                                 (if cut (concat "--by=abs-error:" cut) "--by=abs-error")))))

(defun triage-postmodel-crosstabs-near-miss (model args)
  "Compare MODEL's list at the cut -p of ARGS with the rows just below it.
It runs in a process buffer."
  (interactive (list (triage--read-target :model "Model: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((model (triage-postmodel--id model)))
    (triage--run-in-buffer (format "crosstabs %s" model)
                           (append (list "postmodel" "crosstabs" model "--against=near-miss")
                                   (triage-postmodel--args "crosstabs" args)))))

(defun triage-postmodel-compare (models args)
  "Compare the lists of the two MODELS with the flags ARGS, in a process buffer.
With --sweep the lists are compared at each of its cuts, and a -p
in ARGS is left out, since the CLI takes one or the other."
  (interactive (list (list (triage--read-target :model "First model: ")
                           (read-string "Second model: "))
                     (transient-args 'triage-postmodel-menu)))
  (let* ((ids (mapcar #'triage-postmodel--id models))
         (args (triage-postmodel--args "compare" args))
         (args (if (triage-transient--has-flag-p args "--sweep")
                   (seq-remove (lambda (a) (string-prefix-p "--parameter" a)) args)
                 args)))
    (triage--run-in-buffer (format "compare %s" (string-join ids " "))
                           (append (list "postmodel" "compare") ids args))))

(defun triage-postmodel-baseline (group args)
  "Show where the model GROUP beats its baseline, with the flags ARGS.
The Experiment at point is passed as --experiment unless ARGS name
one.  The view draws the result when the groups share one test date
too (the CLI's exit 1), with the chance tables marked as skipped."
  (interactive (list (triage--read-target :group "Model group: ")
                     (transient-args 'triage-postmodel-menu)))
  (let* ((group (triage-postmodel--id group))
         (args (triage-postmodel--args "baseline" args))
         (hash (triage--at-point :hash))
         (args (if (or (null hash) (triage-transient--has-flag-p args "--experiment"))
                   args
                 (append args (list (concat "--experiment=" hash))))))
    (triage-postmodel--show (format "baseline %s" group)
                            (append (list "postmodel" "baseline" group "--json") args)
                            #'triage-postmodel--insert-baseline
                            (triage-postmodel--context :group group)
                            '(1))))

(defun triage-postmodel-importance (group args)
  "Show each feature family's share of the importance of GROUP.
ARGS are the menu's flags."
  (interactive (list (triage--read-target :group "Model group: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((group (triage-postmodel--id group)))
    (triage-postmodel--show (format "importance %s" group)
                            (append (list "postmodel" "importance" group "--json")
                                    (triage-postmodel--args "importance" args))
                            #'triage-postmodel--insert-importance
                            (triage-postmodel--context :group group))))

(defun triage-postmodel-compare-rows (hash args)
  "Show each model group's error on its own rows and on the common rows of HASH.
ARGS are the menu's flags; --groups keeps some groups only."
  (interactive (list (triage--read-target :hash "Experiment: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((type (triage--problem-type-of-hash hash)))
    (triage-table--show (format "compare-rows %s" (triage--short hash))
                        (append (list "postmodel" "compare-rows" hash "--json")
                                (triage-postmodel--args "compare-rows" args))
                        :on-ret (lambda (row) (triage-model-show (alist-get 'model_id row)))
                        :row-context (lambda (row) (list :model (alist-get 'model_id row)
                                                         :group (alist-get 'model_group_id row)
                                                         :hash hash :type type)))))

(defun triage-postmodel-sensitivity (hash args)
  "Show the metric for each value of one hyperparameter in the Experiment HASH.
ARGS are the menu's flags; the hyperparameter is asked for when they
have no --param."
  (interactive (list (triage--read-target :hash "Experiment: ")
                     (transient-args 'triage-postmodel-menu)))
  (let ((args (triage-postmodel--ensure-flag (triage-postmodel--args "sensitivity" args)
                                             "--param"
                                             (lambda () (read-string "Hyperparameter: ")))))
    (triage-postmodel--show (format "sensitivity %s" (triage--short hash))
                            (append (list "postmodel" "sensitivity" hash "--json") args)
                            #'triage-postmodel--insert-sensitivity
                            (list :hash hash :type (triage--problem-type-of-hash hash)))))

;;;; Text helpers

(defun triage-postmodel--share (value)
  "Return the share VALUE as a percentage, or a dash."
  (if (numberp value) (format "%.1f%%" (* 100 value)) "—"))

(defun triage-postmodel--number (value)
  "Return VALUE with four significant digits, or a dash."
  (if (numberp value) (format "%.4g" value) "—"))

(defun triage-postmodel--share-of (key)
  "Return a column getter: the row's KEY as a percentage."
  (lambda (row) (triage-postmodel--share (alist-get key row))))

(defun triage-postmodel--number-of (key)
  "Return a column getter: the row's KEY with four digits."
  (lambda (row) (triage-postmodel--number (alist-get key row))))

(defun triage-postmodel--by-date (rows)
  "Return ROWS grouped by as_of_date, as ((DATE . ROWS) ...) in their order."
  (let (groups)
    (dolist (row rows)
      (let* ((date (alist-get 'as_of_date row))
             (cell (assoc date groups)))
        (if cell (push row (cdr cell)) (push (list date row) groups))))
    (mapcar (lambda (g) (cons (car g) (reverse (cdr g)))) (nreverse groups))))

(defun triage-postmodel--structure-text (row)
  "Return the forecast structure of ROW in one line, as the CLI prints it."
  (format "%s distinct values; %s of rows at %s; minimum %s, maximum %s; %s negative"
          (triage--str (alist-get 'n_distinct_scores row))
          (triage-postmodel--share (alist-get 'mode_share row))
          (triage-postmodel--number (alist-get 'mode_score row))
          (triage-postmodel--number (alist-get 'min_score row))
          (triage-postmodel--number (alist-get 'max_score row))
          (triage-postmodel--share (alist-get 'negative_share row))))

(defun triage-postmodel--model-row (row)
  "Return the section of a ROW that names a model, so RET opens it."
  (let ((model (alist-get 'model_id row)))
    (list 'triage-model model (list :model model))))

;;;; Residuals

(defun triage-postmodel--insert-residuals (payload)
  "Insert the tables of postmodel residuals --json PAYLOAD, one per part."
  (let ((dates (alist-get 'dates payload)))
    (magit-insert-section (triage-residuals-summary)
      (magit-insert-heading (format "Residuals of model %s"
                                    (triage--str (alist-get 'model_id payload))))
      (triage--key-line "forecast quantile"
                        (or (alist-get 'forecast_quantile payload)
                            "none declared: y ≤ ŷ is the point coverage"))
      (when-let* ((latest (car (last dates))))
        (triage--key-line "forecast structure"
                          (format "on %s, %s" (triage--str (alist-get 'as_of_date latest))
                                  (triage-postmodel--structure-text latest))))
      (insert "\n"))
    (when (assq 'dates payload)
      (triage--insert-table
       'triage-residual-dates "Errors per test date" dates
       `(("as_of_date" as_of_date) ("n" n) ("Σy" sum_outcome) ("MAE" mae) ("RMSE" rmse)
         ("WAPE" wape) ("bias" bias)
         ("top 100 |e|" ,(triage-postmodel--share-of 'top100_error_share))
         ("top 1% |e|" ,(triage-postmodel--share-of 'top1pct_error_share))
         ("y ≤ ŷ" ,(triage-postmodel--share-of 'coverage_point)))))
    (dolist (cut (alist-get 'segments payload))
      (triage--insert-table
       (intern (format "triage-residuals-%s" (car cut)))
       (format "Errors by %s, pooled over the dates" (car cut)) (cdr cut)
       `(("segment" segment) ("rows" n) ("entities" entities) ("Σy" sum_outcome)
         ("MAE" mae) ("WAPE" wape) ("bias" bias)
         ("|e| share" ,(triage-postmodel--share-of 'share_of_abs_error)))))
    (when (assq 'largest_errors payload)
      (triage--insert-table
       'triage-residual-largest "Largest errors" (alist-get 'largest_errors payload)
       '(("entity_id" entity_id) ("label" label) ("as_of_date" as_of_date)
         ("forecast" forecast) ("outcome" outcome) ("error" error))))))

;;;; Baseline

(defun triage-postmodel--insert-wins (title wins)
  "Insert the entity WINS under TITLE as key lines."
  (when wins
    (magit-insert-section ((eval (intern (format "triage-baseline-%s" title))))
      (magit-insert-heading title)
      (triage--key-line "level" (alist-get 'level wins))
      (triage--key-line "count" (alist-get 'n wins))
      (triage--key-line "won by the model" (triage-postmodel--share (alist-get 'won_share wins)))
      (triage--key-line "ties" (triage-postmodel--share (alist-get 'tie_share wins)))
      (triage--key-line "Σy of the won" (triage-postmodel--share
                                         (alist-get 'outcome_share_won wins)))
      (triage--key-line "median |e| ratio" (alist-get 'median_error_ratio wins))
      (when-let* ((unmapped (alist-get 'n_unmapped_rows wins)))
        (triage--key-line "rows in no group" unmapped))
      (insert "\n"))))

(defun triage-postmodel--insert-baseline (payload)
  "Insert the result of postmodel baseline --json PAYLOAD."
  (let ((chance (alist-get 'chance payload))
        (out (alist-get 'out_of_time payload)))
    (magit-insert-section (triage-baseline-summary)
      (magit-insert-heading
        (format "Group %s against baseline group %s" (triage--str (alist-get 'model_group_id payload))
                (triage--str (alist-get 'baseline_group_id payload))))
      (triage--key-line "experiment" (alist-get 'experiment_hash payload) 'triage-dim)
      (triage--key-line "baseline" (if (eq (alist-get 'baseline_chosen payload) t)
                                       "chosen: the lowest MAE on the shared rows"
                                     "given with --baseline"))
      (triage--key-line "shared rows" (alist-get 'n_rows payload))
      (triage--key-line "test dates" (mapconcat #'triage--str (alist-get 'dates payload) ", "))
      (triage--key-line "segments won" (triage-postmodel--share
                                        (alist-get 'segment_win_share payload)))
      (insert "\n"))
    (triage--insert-table
     'triage-baseline-segments "Model against baseline, per segment"
     (alist-get 'segments payload)
     `(("cut" cut) ("segment" segment) ("rows" n) ("Σy" sum_outcome)
       ("MAE model" mae_m) ("MAE base" mae_b) ("WAPE model" wape_m) ("WAPE base" wape_b)
       ("bias model" bias_m) ("closer" ,(triage-postmodel--share-of 'closer_share))
       ("wins" ,(lambda (r) (if (eq (alist-get 'model_wins r) t) "yes" "no")))
       ("excess |e|" excess_abs_error)
       ("excess share" ,(triage-postmodel--share-of 'excess_share))))
    (triage-postmodel--insert-wins "Entities" (alist-get 'entities payload))
    (triage-postmodel--insert-wins "Entity groups" (alist-get 'entity_groups payload))
    (magit-insert-section (triage-baseline-chance)
      (magit-insert-heading "Wins against chance")
      (if (null chance)
          (insert "  " (propertize "skipped: the groups share fewer than two test dates"
                                   'font-lock-face 'triage-warning)
                  "\n\n")
        (triage--key-line "entities" (format "%s, on all %s dates"
                                             (triage--str (alist-get 'n_entities chance))
                                             (triage--str (alist-get 'n_dates chance))))
        (triage--key-line "chi-square" (if (alist-get 'p_value chance)
                                           (format "%s on %s df, p = %s"
                                                   (triage-postmodel--number
                                                    (alist-get 'chi_square chance))
                                                   (triage--str (alist-get 'dof chance))
                                                   (triage-postmodel--number
                                                    (alist-get 'p_value chance)))
                                         "one cell after merging: no test"))
        (triage--key-line "P(win last date)" (triage-postmodel--share
                                              (alist-get 'p_win_last chance)))
        (triage--key-line "P(… | won earlier)"
                          (format "%s over %s entities"
                                  (triage-postmodel--share
                                   (alist-get 'p_win_last_given_all_earlier chance))
                                  (triage--str (alist-get 'n_won_all_earlier chance))))
        (insert "\n")))
    (when chance
      (triage--insert-table 'triage-baseline-cells "Dates won per entity"
                            (alist-get 'cells chance)
                            '(("dates won" wins) ("observed" observed)
                              ("expected by chance" expected))))
    (when out
      (magit-insert-section (triage-baseline-out-of-time)
        (magit-insert-heading (format "Out of time, on %s" (triage--str (alist-get 'last_date out))))
        (triage--key-line "leaders" (format "%s entities the model won before"
                                            (triage--str (alist-get 'n_leaders out))))
        (triage--key-line "rows" (alist-get 'n_rows out))
        (triage--key-line "MAE model" (alist-get 'mae_m out))
        (triage--key-line "MAE baseline" (alist-get 'mae_b out))
        (insert "\n")))))

;;;; Structure

(defun triage-postmodel--insert-structure (payload)
  "Insert the result of postmodel structure --json PAYLOAD."
  (let ((structure (alist-get 'forecast_structure payload))
        (support (alist-get 'leaf_support payload))
        (shift (alist-get 'mean_shift payload)))
    (magit-insert-section (triage-structure-summary)
      (magit-insert-heading (format "Structure of model %s (%s)"
                                    (triage--str (alist-get 'model_id payload))
                                    (triage--str (alist-get 'estimator payload))))
      (when structure
        (triage--key-line "forecast structure"
                          (format "on %s, %s" (triage--str (alist-get 'as_of_date structure))
                                  (triage-postmodel--structure-text structure))))
      (dolist (reason (alist-get 'unavailable payload))
        (insert "  " (propertize (triage--str reason) 'font-lock-face 'triage-warning) "\n"))
      (insert "\n"))
    (when support
      (let ((per (alist-get 'leaves_per_tree support)))
        (magit-insert-section (triage-structure-leaves)
          (magit-insert-heading "Leaf support")
          (triage--key-line "trees" (alist-get 'n_trees support))
          (triage--key-line "leaves per tree" (format "%s to %s, mean %s"
                                                      (triage--str (alist-get 'min per))
                                                      (triage--str (alist-get 'max per))
                                                      (triage--str (alist-get 'mean per))))
          (triage--key-line "small leaves" (format "%s on fewer than %s training rows, in %s tree(s)"
                                                   (triage--str (alist-get 'small_leaves support))
                                                   (triage--str (alist-get 'min_rows support))
                                                   (triage--str (alist-get 'trees_with_small_leaf
                                                                           support))))
          (insert "\n")))
      (triage--insert-table 'triage-structure-small "Leaves on few training rows"
                            (alist-get 'small support)
                            '(("tree" tree) ("node" node) ("rows" n_rows) ("value" value))))
    (when shift
      (magit-insert-section (triage-structure-shift)
        (magit-insert-heading "Mean shift from train to test")
        (triage--key-line "mean forecast train" (alist-get 'mean_forecast_train shift))
        (triage--key-line "mean forecast test" (alist-get 'mean_forecast_test shift))
        (triage--key-line "stored mean test" (alist-get 'stored_mean_test shift))
        (triage--key-line "check gap" (triage-postmodel--number (alist-get 'check_error shift)))
        (triage--key-line "shift" (alist-get 'shift shift))
        (insert "\n"))
      (triage--insert-table 'triage-structure-families "Mean shift by feature family"
                            (alist-get 'families shift)
                            `(("family" family) ("features" n_features)
                              ("β·Δx̄" contribution)
                              ("|share|" ,(triage-postmodel--share-of 'share))))
      (triage--insert-table 'triage-structure-features "Feature contributions"
                            (alist-get 'features shift)
                            '(("feature" feature) ("β" beta) ("x̄ train" mean_train)
                              ("x̄ test" mean_test) ("β·Δx̄" contribution))))))

;;;; Survival

(defun triage-postmodel--population (rows)
  "Return the population row (bin 0) of the survival ROWS."
  (seq-find (lambda (r) (eql (alist-get 'bin r) 0)) rows))

(defun triage-postmodel--extra (key)
  "Return a column getter of the row's extra KEY."
  (lambda (row) (alist-get key (alist-get 'extra row))))

(defun triage-postmodel--insert-survival-date (model estimator day)
  "Insert the survival diagnostics of MODEL (ESTIMATOR) on one DAY."
  (let* ((kinds (alist-get 'kinds day))
         (horizon (triage-postmodel--number (alist-get 'horizon day)))
         (pop (triage-postmodel--population (alist-get 'km_decile kinds)))
         (logrank (car (alist-get 'logrank kinds)))
         (uno (car (alist-get 'uno_c kinds)))
         (calibration (triage-postmodel--population (alist-get 'calibration kinds)))
         (brier (alist-get 'brier kinds))
         (times (mapcar (lambda (r) (alist-get 't r)) (alist-get 'auc_t kinds))))
    (magit-insert-section ((eval (intern (format "triage-survival-%s" (alist-get 'as_of_date day)))))
      (magit-insert-heading (format "Model %s (%s) on %s, horizon %s" model estimator
                                    (triage--str (alist-get 'as_of_date day)) horizon))
      (when pop
        (triage--key-line "labeled rows" (alist-get 'n pop))
        (triage--key-line "events by horizon" (alist-get 'n_events (alist-get 'extra pop)))
        (triage--key-line "KM P(event)" (triage-postmodel--share (alist-get 'value pop))))
      (when (alist-get 'value logrank)
        (triage--key-line "log-rank, tertiles" (format "p = %s"
                                                       (triage-postmodel--number
                                                        (alist-get 'value logrank)))))
      (when (alist-get 'value uno)
        (triage--key-line "Uno's C" (alist-get 'value uno)))
      (when (alist-get 'extra calibration)
        (triage--key-line "calibration" (format "ICI %s, E50 %s"
                                                (triage-postmodel--number
                                                 (alist-get 'ici (alist-get 'extra calibration)))
                                                (triage-postmodel--number
                                                 (alist-get 'e50 (alist-get 'extra calibration))))))
      (triage--key-line "KM steps" (format "%d rows (triage plot km draws them)"
                                           (length (alist-get 'km_group kinds))))
      (dolist (kind kinds)
        (let ((rows (cdr kind)))
          (when (and rows (seq-every-p (lambda (r) (null (alist-get 'value r))) rows))
            (insert "  " (propertize
                          (format "%s undefined: %s" (car kind)
                                  (string-join
                                   (seq-uniq (mapcar (lambda (r) (or (alist-get 'undefined
                                                                                (alist-get 'extra r))
                                                                     "no value"))
                                                     rows))
                                   "; "))
                          'font-lock-face 'triage-warning)
                    "\n"))))
      (insert "\n")
      (triage--insert-table
       (intern (format "triage-survival-deciles-%s" (alist-get 'as_of_date day)))
       (format "Risk deciles at horizon %s" horizon)
       (seq-remove (lambda (r) (eql (alist-get 'bin r) 0)) (alist-get 'km_decile kinds))
       `(("decile" bin) ("rows" n) ("events" ,(triage-postmodel--extra 'n_events))
         ("KM P(event)" ,(triage-postmodel--share-of 'value))
         ("lift" ,(triage-postmodel--extra 'lift))
         ("mean score" ,(triage-postmodel--extra 'mean_score))))
      (triage--insert-table
       (intern (format "triage-survival-time-%s" (alist-get 'as_of_date day)))
       "Over time"
       (mapcar (lambda (time)
                 (let ((auc (seq-find (lambda (r) (equal (alist-get 't r) time))
                                      (alist-get 'auc_t kinds)))
                       (b (seq-find (lambda (r) (equal (alist-get 't r) time)) brier)))
                   `((t . ,time) (auc . ,(alist-get 'value auc)) (brier . ,(alist-get 'value b))
                     (km . ,(alist-get 'km_reference (alist-get 'extra b))))))
               times)
       '(("t" t) ("AUC(t)" auc) ("Brier(t)" brier) ("Brier, training KM" km)))
      (triage--insert-table
       (intern (format "triage-survival-calibration-%s" (alist-get 'as_of_date day)))
       (format "Calibration at horizon %s, per decile of predicted S" horizon)
       (seq-remove (lambda (r) (eql (alist-get 'bin r) 0)) (alist-get 'calibration kinds))
       `(("decile" bin) ("rows" n) ("predicted S" ,(triage-postmodel--extra 'predicted))
         ("KM S" value) ("|gap|" ,(triage-postmodel--extra 'gap)))))))

(defun triage-postmodel--insert-survival (payload)
  "Insert the result of postmodel survival --json PAYLOAD, one section a date."
  (let ((model (triage--str (alist-get 'model_id payload)))
        (estimator (triage--str (alist-get 'estimator payload))))
    (magit-insert-section (triage-survival-summary)
      (magit-insert-heading (format "Survival diagnostics of model %s" model))
      (triage--key-line "estimator" estimator)
      (triage--key-line "horizon" (triage-postmodel--number (alist-get 'horizon payload)))
      (triage--key-line "times" (mapconcat #'triage--str (alist-get 'times payload) ", "))
      (insert "\n"))
    (dolist (day (alist-get 'dates payload))
      (when day
        (triage-postmodel--insert-survival-date model estimator day)))))

;;;; Prototypes

(defun triage-postmodel--deviations (row n)
  "Return the first N deviations of the prototype ROW as text."
  (let ((deviations (seq-take (alist-get 'deviations row) n)))
    (if (null deviations)
        "—"
      (mapconcat (lambda (d) (format "%s %+.2fσ" (alist-get 'feature d)
                                     (or (alist-get 'deviation d) 0)))
                 deviations ", "))))

(defun triage-postmodel--insert-prototypes (payload)
  "Insert the prototypes of postmodel prototypes --json PAYLOAD by date."
  (if (null payload)
      (insert (propertize "No prototype at this cut.\n" 'font-lock-face 'triage-dim))
    (dolist (day (triage-postmodel--by-date payload))
      (triage--insert-table
       (intern (format "triage-prototypes-%s" (car day)))
       (format "Prototypes at %s, top %s" (triage--str (car day))
               (triage--str (alist-get 'parameter (cadr day))))
       (cdr day)
       `(("class" ,(lambda (r) (upcase (triage--str (alist-get 'class r)))))
         ("rows" class_size) ("rank" rank) ("entity" entity_id) ("distance" distance)
         ("largest deviations" ,(lambda (r) (triage-postmodel--deviations r 3))))))))

;;;; Importance and sensitivity

(defun triage-postmodel--insert-importance (payload)
  "Insert the result of postmodel importance --json PAYLOAD."
  (let* ((models (alist-get 'models payload))
         (families (seq-uniq (seq-mapcat (lambda (m) (mapcar #'car (alist-get 'families m)))
                                         models))))
    (magit-insert-section (triage-importance-summary)
      (magit-insert-heading (format "Importance by family of group %s"
                                    (triage--str (alist-get 'model_group_id payload))))
      (triage--key-line "features compared" (format "the top %s between train ends"
                                                    (triage--str (alist-get 'top payload))))
      (insert "\n"))
    (triage--insert-table
     'triage-importance-models "Share of Σ|importance| by family, per model" models
     (append '(("model" model_id) ("train end" train_end_time))
             (mapcar (lambda (family)
                       (list (symbol-name family)
                             (lambda (m) (triage-postmodel--share
                                          (alist-get family (alist-get 'families m))))))
                     (seq-take families 8)))
     #'triage-postmodel--model-row)
    (triage--insert-table 'triage-importance-stability "Stability between train ends"
                          (alist-get 'stability payload)
                          '(("from model" previous_model_id) ("to model" model_id)
                            ("top-k Jaccard" jaccard)))))

(defun triage-postmodel--insert-sensitivity (payload)
  "Insert the result of postmodel sensitivity --json PAYLOAD."
  (let ((hash (alist-get 'experiment_hash payload)))
    (magit-insert-section (triage-sensitivity-summary)
      (magit-insert-heading (format "%s: %s by %s" (triage--str (alist-get 'model_type payload))
                                    (triage--str (alist-get 'metric payload))
                                    (triage--str (alist-get 'param payload))))
      (triage--key-line "experiment" hash 'triage-dim)
      (triage--key-line "parameter" (alist-get 'parameter payload))
      (triage--key-line "rows" (if (string-empty-p (or (alist-get 'subset_hash payload) ""))
                                   "each group's own"
                                 (format "common rows, Subset %s"
                                         (triage--short (alist-get 'subset_hash payload)))))
      (insert "\n"))
    (triage--insert-table
     'triage-sensitivity-rows "Groups" (alist-get 'rows payload)
     `((,(triage--str (alist-get 'param payload)) value) ("group" model_group_id)
       ("mean" mean_metric) ("dates" n_dates))
     (lambda (row) (let ((group (alist-get 'model_group_id row)))
                     (list 'triage-model-group group (list :group group :hash hash)))))))

(provide 'triage-postmodel)

;;; triage-postmodel.el ends here
