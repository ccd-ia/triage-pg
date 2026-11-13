;;; triage-transient.el --- Transient menus over the triage CLI  -*- lexical-binding: t; -*-

;; Copyright (c) 2026 Adolfo De Unánue and triage-pg contributors
;; SPDX-License-Identifier: MIT

;; This file is not part of GNU Emacs.

;;; Commentary:

;; `triage-dispatch' and the menus it opens.  Each suffix builds the
;; argument list of one existing CLI verb and starts it.  The config,
;; Experiment, Run, model or model group at point fills the target;
;; otherwise the suffix asks for one.  Each infix maps to one flag of
;; the verb.
;;
;; triage reset is in no menu.  It is reachable through `triage-actions'
;; (!), where the CLI's own confirmation applies.

;;; Code:

(require 'triage)
(require 'transient)

(declare-function triage-json-show "triage-object" (name args))
(declare-function triage-model-group-show "triage-object" (hash group))
(declare-function triage-table--show "triage-object" (name args &rest keys))
(declare-function triage-model-show "triage-object" (model-id))
(declare-function vterm "vterm" (&optional arg))
(declare-function make-term "term" (name program &optional startfile &rest switches))
(declare-function term-mode "term" ())
(declare-function term-char-mode "term" ())
(defvar vterm-shell)
(defvar transient--original-buffer)

;;;; Targets and flags

(defun triage-transient--buffer ()
  "Return the buffer a menu was opened from."
  (or (and (boundp 'transient--original-buffer)
           (buffer-live-p transient--original-buffer)
           transient--original-buffer)
      (current-buffer)))

(defun triage-transient--lacks (&rest kinds)
  "Return non-nil when no item of any of KINDS is at hand.
At hand means at point or offered by a prompt."
  (with-current-buffer (triage-transient--buffer)
    (not (seq-some #'triage--has-target-p kinds))))

(defun triage-transient--no-hash-p ()
  "Return non-nil when no Experiment is at hand."
  (triage-transient--lacks :hash))

(defun triage-transient--no-model-p ()
  "Return non-nil when no model is at hand."
  (triage-transient--lacks :model))

(defun triage-transient--no-group-p ()
  "Return non-nil when no model group is at hand."
  (triage-transient--lacks :group))

(defun triage-transient--no-plot-target-p ()
  "Return non-nil when neither a model nor a model group is at hand."
  (triage-transient--lacks :model :group))

(defun triage-transient--only (args flags)
  "Return the elements of ARGS that are one of FLAGS.
FLAGS are flag names such as \"--date\"; an element matches as
\"--date\" or \"--date=VALUE\"."
  (seq-filter (lambda (arg)
                (seq-some (lambda (flag) (or (equal arg flag) (string-prefix-p (concat flag "=") arg)))
                          flags))
              args))

(defun triage-transient--has-flag-p (args flag)
  "Return non-nil if ARGS carry FLAG, bare or with a value."
  (triage-transient--only args (list flag)))

(defun triage-transient--temp (suffix)
  "Return a new temporary file name ending in SUFFIX."
  (make-temp-file "triage-" nil suffix))

;;;; Images

(defvar-local triage--temp-file nil
  "The temporary file this buffer shows; deleted with the buffer.")

(defun triage--delete-temp-file ()
  "Delete the buffer's temporary file, if it still exists."
  (when (and triage--temp-file (file-exists-p triage--temp-file))
    (delete-file triage--temp-file)))

(defun triage--show-image (file title)
  "Show the image FILE in the buffer *triage: TITLE*.
FILE is deleted when the buffer is killed or shows another image."
  (if (not (and (file-exists-p file) (> (file-attribute-size (file-attributes file)) 0)))
      (message "triage: %s wrote no image" title)
    (let ((buffer (get-buffer-create (format "*triage: %s*" title))))
      (with-current-buffer buffer
        (triage--delete-temp-file)
        (let ((inhibit-read-only t))
          (erase-buffer)
          (set-buffer-multibyte nil)
          (insert-file-contents-literally file))
        (if (display-images-p) (image-mode) (special-mode))
        (setq triage--temp-file file)
        (add-hook 'kill-buffer-hook #'triage--delete-temp-file nil t))
      (pop-to-buffer buffer))))

;;;; run

;;;###autoload (autoload 'triage-run-menu "triage-transient" nil t)
(transient-define-prefix triage-run-menu ()
  "Run an Experiment's config end to end with triage run."
  ["Arguments"
   ("-p" "Profile" "--profile=" :choices ("local" "cloud"))
   ("-s" "Random seed" "--random-seed=" :reader transient-read-number-N0)
   ("-c" "Cache policy" "--cache-policy=" :choices ("exact" "logical"))
   ("-d" "Project path" "--project-path=" :reader transient-read-directory)]
  ["Run"
   ("r" "Run the config" triage-run-config)])

(defun triage-run-config (config args)
  "Run CONFIG with triage run and the flags ARGS, in a process buffer.
The status buffer refreshes when the run ends.  With
--profile=cloud the CLI only submits the job to AWS Batch."
  (interactive (list (triage--read-target :config "Config to run: ")
                     (transient-args 'triage-run-menu)))
  (let ((cloud (member "--profile=cloud" args)))
    (triage--run-in-buffer
     (format "run %s" (file-name-nondirectory config))
     (append (list "run" config) args)
     :on-exit (lambda (code)
                (when (and cloud (zerop code))
                  (message "triage: the run was submitted to AWS Batch; it runs there"))
                (triage--refresh-status-buffer)))))

;;;; check

;;;###autoload (autoload 'triage-check-menu "triage-transient" nil t)
(transient-define-prefix triage-check-menu ()
  "Check an Experiment's config for leakage with triage check leakage."
  ["Arguments"
   ("-d" "Only these as_of_dates (D1,D2)" "--dates=")]
  ["Check"
   ("c" "Leakage check" triage-check-leakage)])

(defun triage-check-leakage (config args)
  "Check CONFIG for leakage with the flags ARGS.
Exit 0 says there are no differences, exit 1 opens the report and
any other exit shows the CLI's error."
  (interactive (list (triage--read-target :config "Config to check: ")
                     (transient-args 'triage-check-menu)))
  (let* ((report (triage-transient--temp ".json"))
         (args (append (list "check" "leakage" config "--json" report) args))
         (command (append (triage--executable) args)))
    (message "triage: checking %s for leakage…" config)
    (triage--call
     args
     (lambda (code stdout stderr)
       (pcase code
         (0 (delete-file report)
            (message "triage: leakage check of %s found no differences" config))
         (1 (triage-leakage-report config report))
         (_ (when (file-exists-p report) (delete-file report))
            (triage--report-error command code (concat stdout stderr) nil)))))))

(define-derived-mode triage-leakage-mode triage-section-mode "Triage-Leakage"
  "Mode for the report of a leakage check."
  :group 'triage)

(defun triage-leakage-report (config file)
  "Show the leakage report of CONFIG that the CLI wrote to FILE, then delete FILE."
  (let ((payload (with-temp-buffer
                   (insert-file-contents file)
                   (triage--parse-json (buffer-string))))
        (state (triage--capture))
        (buffer (get-buffer-create (format "*triage: leakage %s*" (file-name-nondirectory config)))))
    (delete-file file)
    (with-current-buffer buffer
      (triage-leakage-mode)
      (triage--restore state)
      (setq triage--payload payload)
      (triage--redraw 'triage-leakage (lambda () (triage-leakage--insert config payload))))
    (pop-to-buffer buffer)
    buffer))

(defun triage-leakage--insert (config payload)
  "Insert the leakage report PAYLOAD of CONFIG."
  (if (eq payload 'invalid)
      (insert (propertize "The report file is not JSON.\n" 'font-lock-face 'triage-danger))
    (magit-insert-section (triage-leakage-summary)
      (magit-insert-heading (format "Leakage check of %s" config))
      (triage--key-line "leakage found" (alist-get 'leakage_found payload)
                        (if (eq (alist-get 'leakage_found payload) t) 'triage-danger 'triage-ok))
      (triage--key-line "dates checked" (mapconcat #'triage--str (alist-get 'dates_checked payload) ", "))
      (triage--key-line "sources cut" (mapconcat #'triage--str (alist-get 'sources_cut payload) ", "))
      (triage--key-line "elapsed (s)" (alist-get 'elapsed_s payload))
      (insert "\n"))
    (let ((findings (alist-get 'findings payload)))
      (magit-insert-section (triage-leakage-findings)
        (magit-insert-heading (format "Findings (%d)" (length findings)))
        (seq-do-indexed
         (lambda (finding i)
           (magit-insert-section (triage-leakage-finding i)
             (insert (format "  %-10s %-10s %-10s +%s -%s ~%s  %s entities  %s\n"
                             (triage--str (alist-get 'component finding))
                             (triage--str (alist-get 'as_of_date finding))
                             (triage--str (alist-get 'label_timespan finding))
                             (triage--str (alist-get 'added finding))
                             (triage--str (alist-get 'removed finding))
                             (triage--str (alist-get 'changed finding))
                             (triage--str (alist-get 'n_entities finding))
                             (triage--str (alist-get 'cut finding))))
             (when-let* ((examples (alist-get 'examples finding)))
               (insert "    examples: " (mapconcat #'triage--str examples ", ") "\n"))))
         findings)
        (insert "\n")))
    (when-let* ((not-checked (alist-get 'not_checked payload)))
      (magit-insert-section (triage-leakage-not-checked)
        (magit-insert-heading (format "Not checked (%d)" (length not-checked)))
        (dolist (item not-checked)
          (insert "  " (triage--str item) "\n"))))))

;;;; analyze

;;;###autoload (autoload 'triage-analyze-menu "triage-transient" nil t)
(transient-define-prefix triage-analyze-menu ()
  "Show the plan a config implies with triage analyze-config."
  ["Arguments"
   ("-e" "Estimate from the database" "--estimate")
   ("-n" "Estimate only the first N dates" "--estimate-dates=" :reader transient-read-number-N0)
   ("-f" "Feature columns matching" "--features=")
   ("-p" "Plot the temporal splits" "--plot")]
  ["Analyze"
   ("a" "Analyze the config" triage-analyze-config)])

(defun triage-analyze-config (config args)
  "Run triage analyze-config on CONFIG with the flags ARGS.
With --plot, the splits are drawn to a temporary SVG that opens
when the command ends."
  (interactive (list (triage--read-target :config "Config to analyze: ")
                     (transient-args 'triage-analyze-menu)))
  (let* ((plot (and (member "--plot" args) (triage-transient--temp ".svg")))
         (args (append (list "analyze-config" config)
                       (remove "--plot" args)
                       (and plot (list "--plot" plot)))))
    (triage--run-in-buffer
     (format "analyze %s" (file-name-nondirectory config))
     args
     :on-exit (lambda (code)
                (when plot
                  (if (zerop code)
                      (triage--show-image plot (format "splits %s" (file-name-nondirectory config)))
                    (when (file-exists-p plot) (delete-file plot))))))))

;;;; results

;;;###autoload (autoload 'triage-results-menu "triage-transient" nil t)
(transient-define-prefix triage-results-menu ()
  "Read an Experiment's results: leaderboard, audition, model groups."
  ["Arguments"
   ("-m" "Metric" "--metric=")
   ("-p" "Parameter" "--parameter=")
   ("-w" "Windowed (leaderboard)" "--windowed")]
  ["Results"
   ("l" "Leaderboard" triage-leaderboard :inapt-if triage-transient--no-hash-p)
   ("R" "Refresh the leaderboard" triage-leaderboard-refresh :inapt-if triage-transient--no-hash-p)
   ("u" "Audition" triage-audition :inapt-if triage-transient--no-hash-p)
   ("M" "Model groups" triage-model-groups :inapt-if triage-transient--no-hash-p)])

(defun triage--leaderboard-args (hash args &optional refresh)
  "Return the leaderboard arguments for HASH with the flags ARGS.
REFRESH non-nil adds --refresh."
  (append (list "leaderboard" hash "--json")
          (and refresh (list "--refresh"))
          (triage-transient--only args '("--metric" "--parameter" "--windowed"))))

(defun triage--leaderboard-table (hash args name)
  "Show the leaderboard rows of triage ARGS for HASH in the table NAME."
  (triage-table--show name args
                      :on-ret (lambda (row) (triage-model-show (alist-get 'model_id row)))
                      :row-context (lambda (row) (list :model (alist-get 'model_id row)
                                                       :group (alist-get 'model_group_id row)
                                                       :hash hash))))

(defun triage-leaderboard (hash args)
  "Show the leaderboard of the Experiment HASH, filtered by the flags ARGS."
  (interactive (list (triage--read-target :hash "Experiment: ")
                     (transient-args 'triage-results-menu)))
  (triage--leaderboard-table hash (triage--leaderboard-args hash args)
                             (format "leaderboard %s" (triage--short hash))))

(defun triage-leaderboard-refresh (hash args)
  "Refresh the leaderboard matview, then show the leaderboard of HASH.
ARGS are the flags of the results menu.  The status buffer refreshes
too, since its pending list counts runs missing from the matview."
  (interactive (list (triage--read-target :hash "Experiment: ")
                     (transient-args 'triage-results-menu)))
  (prog1 (triage--leaderboard-table hash (triage--leaderboard-args hash args t)
                                    (format "leaderboard %s" (triage--short hash)))
    (triage--refresh-status-buffer)))

(defun triage-audition (hash args)
  "Show the audition of the Experiment HASH with the flags ARGS."
  (interactive (list (triage--read-target :hash "Experiment: ")
                     (transient-args 'triage-results-menu)))
  (triage-json-show (format "audition %s" (triage--short hash))
                    (append (list "audition" hash "--json")
                            (triage-transient--only args '("--metric" "--parameter")))))

(defun triage-model-groups (hash args)
  "Show the model groups of the Experiment HASH with the flags ARGS."
  (interactive (list (triage--read-target :hash "Experiment: ")
                     (transient-args 'triage-results-menu)))
  (triage-table--show (format "models %s" (triage--short hash))
                      (append (list "models" hash "--json")
                              (triage-transient--only args '("--metric" "--parameter")))
                      :on-ret (lambda (row) (triage-model-group-show
                                             hash (alist-get 'model_group_id row)))
                      :row-context (lambda (row) (list :group (alist-get 'model_group_id row)
                                                       :hash hash))))

;;;; score

;;;###autoload (autoload 'triage-score-menu "triage-transient" nil t)
(transient-define-prefix triage-score-menu ()
  "Forward-score a model with triage score."
  ["Arguments"
   ("-c" "Production cohort query (SQL file)" "--cohort-query=" :reader transient-read-existing-file)
   ("-d" "Project path" "--project-path=" :reader transient-read-directory)]
  ["Score"
   ("f" "Forward-score the model" triage-score-model :inapt-if triage-transient--no-model-p)])

(defun triage-score-model (model date args)
  "Forward-score MODEL as of DATE with the flags ARGS, in a process buffer.
An empty DATE leaves it out, and the CLI scores as of today."
  (interactive (list (triage--read-target :model "Model to score: ")
                     (read-string "Prediction date (YYYY-MM-DD, empty for today): ")
                     (transient-args 'triage-score-menu)))
  (let ((model (if (numberp model) (number-to-string model) model)))
    (triage--run-in-buffer (format "score %s" model)
                           (append (list "score" model)
                                   (and date (not (string-empty-p date)) (list date))
                                   args)
                           :on-exit #'triage--refresh-status-buffer)))

;;;; plot

(eval-and-compile
(defconst triage-plot-subcommands
  '(("curve" :model ("--date" "--subset" "--compare" "--points"))
    ("error-rules" :model ("--date" "--parameter" "--kind" "--top"))
    ("crosstabs" :model ("--parameter" "--date" "--stat" "--compare" "--top"))
    ("curve-dates" :group ("--subset" "--points"))
    ("leaves" :group ("--parameter") "--parameter")
    ("recurrence" :group ("--parameter" "--kind") "--parameter")
    ("crosstabs-dates" :group ("--parameter" "--stat" "--top"))
    ("overlap" :groups ("--parameter") "--parameter"))
  "Each triage plot subcommand: its target, its flags, its required flag.
Checked against triage plot SUB --help."))

;;;###autoload (autoload 'triage-plot-menu "triage-transient" nil t)
(transient-define-prefix triage-plot-menu ()
  "Draw a model's or a model group's figures with triage plot."
  ["Arguments"
   ("-d" "Test date (YYYY-MM-DD)" "--date=")
   ("-p" "Cut (parameter)" "--parameter=")
   ("-k" "Kind" "--kind=" :choices ("fp" "fn" "confusion"))
   ("-s" "Statistic" "--stat=" :choices ("mean" "nonzero"))
   ("-t" "Top" "--top=" :reader transient-read-number-N+)
   ("-c" "Compare with model" "--compare=" :reader transient-read-number-N+)
   ("-b" "Subset" "--subset=")
   ("-n" "Points per curve" "--points=" :reader transient-read-number-N+)]
  [["Model"
    ("c" "Threshold curve" triage-plot-curve :inapt-if triage-transient--no-model-p)
    ("e" "Error rules" triage-plot-error-rules :inapt-if triage-transient--no-model-p)
    ("x" "Crosstabs" triage-plot-crosstabs :inapt-if triage-transient--no-model-p)]
   ["Model group"
    ("C" "Curve across dates" triage-plot-curve-dates :inapt-if triage-transient--no-group-p)
    ("l" "Leaves" triage-plot-leaves :inapt-if triage-transient--no-group-p)
    ("r" "Recurrence" triage-plot-recurrence :inapt-if triage-transient--no-group-p)
    ("X" "Crosstabs across dates" triage-plot-crosstabs-dates :inapt-if triage-transient--no-group-p)
    ("o" "Overlap of two groups" triage-plot-overlap :inapt-if triage-transient--no-group-p)]])

(defun triage-plot (subcommand targets args)
  "Draw triage plot SUBCOMMAND of TARGETS with the flags ARGS.
Only the flags SUBCOMMAND accepts are passed.  A missing required
flag is read from the minibuffer.  The figure goes to a temporary
SVG that opens in an image buffer."
  (pcase-let* ((`(,_target ,flags ,required) (cdr (assoc subcommand triage-plot-subcommands)))
               (args (triage-transient--only args flags))
               (args (if (and required (not (triage-transient--has-flag-p args required)))
                         (append args (list (concat required "="
                                                    (read-string (format "%s (required): " required)))))
                       args))
               (file (triage-transient--temp ".svg"))
               (call (append (list "plot" subcommand)
                             (mapcar (lambda (v) (if (numberp v) (number-to-string v) v)) targets)
                             args (list "-o" file)))
               (command (append (triage--executable) call)))
    (message "triage: drawing %s…" subcommand)
    (triage--call
     call
     (lambda (code stdout stderr)
       (if (zerop code)
           (triage--show-image file (format "plot %s %s" subcommand
                                            (mapconcat #'triage--str targets " ")))
         (when (file-exists-p file) (delete-file file))
         (triage--report-error command code (concat stdout stderr) nil))))))

(defmacro triage-transient--define-plot (subcommand)
  "Define the suffix that draws triage plot SUBCOMMAND."
  (let* ((spec (cdr (assoc subcommand triage-plot-subcommands)))
         (target (car spec))
         (name (intern (format "triage-plot-%s" subcommand))))
    `(defun ,name (targets args)
       ,(format "Draw triage plot %s of TARGETS with the flags ARGS.\nTARGETS is a list of %s."
                subcommand (pcase target (:model "one model id") (:group "one model group id")
                             (:groups "two model group ids")))
       (interactive
        (list ,(pcase target
                 (:model '(list (triage--read-target :model "Model: ")))
                 (:group '(list (triage--read-target :group "Model group: ")))
                 (:groups '(list (triage--read-target :group "First model group: ")
                                 (read-string "Second model group: "))))
              (transient-args 'triage-plot-menu)))
       (triage-plot ,subcommand targets args))))

(triage-transient--define-plot "curve")
(triage-transient--define-plot "error-rules")
(triage-transient--define-plot "crosstabs")
(triage-transient--define-plot "curve-dates")
(triage-transient--define-plot "leaves")
(triage-transient--define-plot "recurrence")
(triage-transient--define-plot "crosstabs-dates")
(triage-transient--define-plot "overlap")

;;;; maintain

;;;###autoload (autoload 'triage-maintain-menu "triage-transient" nil t)
(transient-define-prefix triage-maintain-menu ()
  "Archive Experiments, collect garbage, upgrade the schema."
  ["Arguments"
   ("-a" "Only artifacts at least N days old" "--min-age=" :reader transient-read-number-N0)
   ("-P" "Also delete the rows (with delete)" "--purge")]
  ["Maintain"
   ("a" "Archive the Experiment" triage-archive-experiment :inapt-if triage-transient--no-hash-p)
   ("g" "Garbage collection, dry run" triage-gc-dry-run)
   ("D" "Garbage collection, delete" triage-gc-delete)
   ("u" "Upgrade the schema" triage-db-upgrade)])

(defun triage-archive-experiment (hash)
  "Archive the Experiment HASH after asking.
An archived Experiment is no longer a root of garbage collection."
  (interactive (list (triage--read-target :hash "Experiment to archive: ")))
  (when (yes-or-no-p (format "Archive Experiment %s, so a later gc may collect its artifacts? "
                             (triage--short hash)))
    (triage--run-in-buffer (format "archive %s" (triage--short hash))
                           (list "archive" hash)
                           :on-exit #'triage--refresh-status-buffer)))

(defun triage-gc-dry-run (args)
  "List what garbage collection would collect, with the flags ARGS."
  (interactive (list (transient-args 'triage-maintain-menu)))
  (triage--run-in-buffer "gc" (cons "gc" (triage-transient--only args '("--min-age")))))

(defun triage-gc-delete (args)
  "Collect unreachable artifacts with the flags ARGS, after asking."
  (interactive (list (transient-args 'triage-maintain-menu)))
  (let ((purge (member "--purge" args)))
    (when (yes-or-no-p (if purge
                           "Collect dead artifacts and delete their rows? "
                         "Collect the outputs of dead artifacts? "))
      (triage--run-in-buffer "gc" (append (list "gc" "--delete")
                                          (triage-transient--only args '("--min-age" "--purge")))
                             :on-exit #'triage--refresh-status-buffer))))

(defun triage-db-upgrade ()
  "Upgrade the project's triage schema to the newest revision."
  (interactive)
  (triage--run-in-buffer "db upgrade" (list "db" "upgrade")
                         :on-exit #'triage--refresh-status-buffer))

;;;; serve

;;;###autoload (autoload 'triage-serve-menu "triage-transient" nil t)
(transient-define-prefix triage-serve-menu ()
  "Start the dashboard with triage serve."
  ["Arguments"
   ("-p" "Port" "--port=" :reader transient-read-number-N+)]
  ["Serve"
   ("s" "Start the dashboard" triage-serve)])

(defun triage-serve (args)
  "Start the dashboard with the flags ARGS and open it once it listens."
  (interactive (list (transient-args 'triage-serve-menu)))
  (triage--run-in-buffer "serve" (cons "serve" (triage-transient--only args '("--port")))
                         :on-match (cons "Uvicorn running on \\(https?://[^ \n]+\\)"
                                         #'browse-url)))

;;;; TUI and actions

;;;###autoload
(defun triage-tui ()
  "Open the triage terminal cockpit in vterm, or in `ansi-term'."
  (interactive)
  (let* ((command (append (triage--executable) (list "tui")))
         (name "*triage: tui*"))
    (if (require 'vterm nil t)
        (triage--with-global-env
          (let ((vterm-shell (mapconcat #'shell-quote-argument command " ")))
            (vterm name)))
      (require 'term)
      (triage--with-global-env
        (let ((buffer (apply #'make-term "triage: tui" (car command) nil (cdr command))))
          (with-current-buffer buffer
            (term-mode)
            (term-char-mode))
          (pop-to-buffer buffer))))))

;;;###autoload
(defun triage-actions ()
  "Run any action the CLI lists: a triage verb or a just recipe."
  (interactive)
  (let ((origin (current-buffer)))
    (triage--call-json
     '("actions" "list" "--json")
     (lambda (actions)
       (when (buffer-live-p origin)
         (with-current-buffer origin
           (triage-actions--choose actions)))))))

(defun triage-actions--choose (actions)
  "Ask for one of ACTIONS and its arguments, then run it."
  (let* ((table (mapcar (lambda (a) (cons (alist-get 'name a) a)) actions))
         (completion-extra-properties
          (list :annotation-function
                (lambda (name)
                  (let ((action (cdr (assoc name table))))
                    (concat (propertize " " 'display '(space :align-to 32))
                            (triage--str (alist-get 'args action))
                            (if (eq (alist-get 'destructive action) t) "  [destructive] " "  ")
                            (triage--str (alist-get 'description action)))))))
         (name (completing-read "Action: " table nil t))
         (action (cdr (assoc name table)))
         (wanted (triage--str (alist-get 'args action)))
         (extra (if (string-empty-p wanted)
                    ""
                  (read-string (format "%s %s: " name wanted)))))
    (triage--run-in-buffer name (append (list "actions" "run" name)
                                        (split-string-and-unquote extra))
                           :on-exit #'triage--refresh-status-buffer)))

;;;; dispatch

;;;###autoload (autoload 'triage-dispatch "triage-transient" nil t)
(transient-define-prefix triage-dispatch ()
  "Every triage menu, and the commands of the current buffer."
  [["Experiment"
    ("r" "Run" triage-run-menu)
    ("c" "Check leakage" triage-check-menu)
    ("a" "Analyze config" triage-analyze-menu)]
   ["Results"
    ("l" "Results" triage-results-menu :inapt-if triage-transient--no-hash-p)
    ("f" "Forward score" triage-score-menu :inapt-if triage-transient--no-model-p)
    ("p" "Plot" triage-plot-menu :inapt-if triage-transient--no-plot-target-p)]
   ["Project"
    ("m" "Maintain" triage-maintain-menu)
    ("s" "Serve the dashboard" triage-serve-menu)
    ("T" "Terminal cockpit" triage-tui)]
   ["Buffer"
    ("g" "Refresh" triage-refresh)
    (":" "Query" triage-query)
    ("!" "Any action" triage-actions)]])

(provide 'triage-transient)

;;; triage-transient.el ends here
