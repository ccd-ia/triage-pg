;;; triage-test.el --- ERT tests for triage.el  -*- lexical-binding: t; -*-

;;; Commentary:

;; The tests read the JSON fixtures in emacs/test/fixtures/, which the
;; pytest shape test keeps in step with live CLI output.  The process
;; starters are stubbed, so no test starts a process.  Run them with
;; `just emacs-test' or emacs --batch -l emacs/test/ci.el.

;;; Code:

(require 'ert)
(require 'cl-lib)
(require 'triage)

(defconst triage-test--fixtures
  (expand-file-name "fixtures/"
                    (file-name-directory (or load-file-name buffer-file-name))))

(defun triage-test-fixture (name)
  "Return the text of the fixture NAME.json."
  (with-temp-buffer
    (insert-file-contents (expand-file-name (concat name ".json") triage-test--fixtures))
    (buffer-string)))

(defun triage-test-payload (name)
  "Return the fixture NAME.json parsed as triage.el parses CLI output."
  (triage--parse-json (triage-test-fixture name)))

;;;; The stub

(defvar triage-test--calls nil
  "Commands the stubbed starter received, newest first.")

(defvar triage-test--defer nil
  "Non-nil means the stub holds each call until `triage-test-deliver'.")

(defvar triage-test--held nil
  "Thunks that complete the calls the stub holds.")

(defun triage-test-deliver ()
  "Complete every call the stub holds."
  (let ((held (reverse triage-test--held)))
    (setq triage-test--held nil)
    (mapc #'funcall held)))

(defmacro triage-test-with-stub (responder &rest body)
  "Run BODY with the process starter replaced by RESPONDER.
RESPONDER is called with the command list and returns (CODE STDOUT
STDERR).  Commands are recorded in `triage-test--calls'."
  (declare (indent 1))
  `(let* ((triage-test--calls nil)
          (triage-test--held nil)
          (responder ,responder)
          (triage--call-function
           (lambda (&rest args)
             (let ((command (plist-get args :command)))
               (push command triage-test--calls)
               (let ((thunk
                      (lambda ()
                        (pcase-let ((`(,code ,out ,err) (funcall responder command)))
                          (with-current-buffer (plist-get args :buffer) (insert out))
                          (with-current-buffer (plist-get args :stderr-buffer) (insert err))
                          (funcall (plist-get args :finish) code)))))
                 (if triage-test--defer
                     (push thunk triage-test--held)
                   (funcall thunk))))))
          (triage-executable '("triage")))
     ,@body))

(defun triage-test-fixture-responder (name)
  "Return a responder that answers every call with fixture NAME."
  (lambda (_command) (list 0 (triage-test-fixture name) "")))

(defmacro triage-test-in-project (&rest body)
  "Run BODY in a temporary buffer that belongs to a temporary project."
  (declare (indent 0))
  `(let* ((root (file-name-as-directory (make-temp-file "triage-test-" t)))
          (default-directory root))
     (unwind-protect
         (with-temp-buffer
           (setq-local triage--root root)
           (setq default-directory root)
           ,@body)
       (delete-directory root t))))

(defun triage-test-write (dir file text)
  "Write TEXT to FILE under DIR, creating directories, and return its path."
  (let ((path (expand-file-name file dir)))
    (make-directory (file-name-directory path) t)
    (with-temp-file path (insert text))
    path))

;;;; Phase 4: detection, executable, the JSON call, the contract

(defmacro triage-test-with-dir (var &rest body)
  "Bind VAR to a fresh temporary directory around BODY."
  (declare (indent 1))
  `(let ((,var (file-name-as-directory (make-temp-file "triage-test-" t))))
     (unwind-protect (progn (triage-clear-caches) ,@body)
       (delete-directory ,var t))))

(ert-deftest triage-test-root-from-pyproject-dependency ()
  (triage-test-with-dir dir
    (triage-test-write dir "pyproject.toml"
                       "[project]\nname = \"inspections\"\ndependencies = [\n  \"triage[dashboard] @ git+https://github.com/ccd-ia/triage-pg.git@v1.6.0\",\n]\n")
    (let ((sub (file-name-directory (triage-test-write dir "experiments/a/x.yaml" ""))))
      (should (equal (triage-project-root sub) dir)))))

(ert-deftest triage-test-root-from-pyproject-name ()
  (triage-test-with-dir dir
    (triage-test-write dir "pyproject.toml" "[project]\nname = 'triage-pg'\nversion = \"1.6.0\"\n")
    (should (equal (triage-project-root dir) dir))))

(ert-deftest triage-test-root-from-a-uv-source-path ()
  (triage-test-with-dir dir
    (triage-test-write dir "pyproject.toml"
                       "[tool.uv.sources]\ntriage = { path = \"../triage-pg\", editable = true }\n")
    (should (equal (triage-project-root dir) dir))))

(ert-deftest triage-test-root-ignores-a-comment ()
  (triage-test-with-dir dir
    (triage-test-write dir "pyproject.toml"
                       "[project]\nname = \"featurizer\"\n  # see 'triage-pg' and https://github.com/ccd-ia/triage-pg\n")
    (should-not (triage-project-root dir))))

(ert-deftest triage-test-root-ignores-similar-names ()
  (triage-test-with-dir dir
    (triage-test-write dir "pyproject.toml" "[project]\nname = \"triage-pg-extras\"\n")
    (should-not (triage-project-root dir))))

(ert-deftest triage-test-root-from-database-yaml ()
  (triage-test-with-dir dir
    (triage-test-write dir "database.yaml" "host: localhost\n")
    (let ((sub (file-name-as-directory (expand-file-name "deep/er" dir))))
      (make-directory sub t)
      (should (equal (triage-project-root sub) dir)))))

(ert-deftest triage-test-no-root-is-a-user-error ()
  (triage-test-with-dir dir
    (let ((triage-project-markers '("no-such-marker.yaml")))
      (should-not (triage-project-root dir))
      (let ((err (should-error (triage--require-root dir) :type 'user-error)))
        (should (string-match-p "No triage project above .*looked for: no-such-marker.yaml"
                                (cadr err)))))))

(ert-deftest triage-test-root-cache-and-clear ()
  (triage-test-with-dir dir
    (should-not (triage-project-root dir))
    (triage-test-write dir "database.yaml" "")
    ;; The cached answer survives until the caches are cleared.
    (should-not (triage-project-root dir))
    (triage-clear-caches)
    (should (equal (triage-project-root dir) dir))))

(defun triage-test-make-exe (dir file)
  "Create an executable FILE under DIR and return its path."
  (let ((path (triage-test-write dir file "#!/bin/sh\n")))
    (set-file-modes path #o755)
    path))

(ert-deftest triage-test-executable-order ()
  (triage-test-with-dir root
    (triage-test-with-dir uv-env
      (let ((triage-executable nil)
            (exec-path nil)
            (process-environment (cons (concat "UV_PROJECT_ENVIRONMENT=" uv-env)
                                       process-environment)))
        ;; Nothing exists: uv run is the last resort.
        (should (equal (triage--find-executable root) '("uv" "run" "triage")))
        ;; triage on exec-path beats uv run.
        (let* ((bin (file-name-as-directory (expand-file-name "bin-on-path" root)))
               (on-path (triage-test-make-exe bin "triage"))
               (exec-path (list bin)))
          (should (equal (triage--find-executable root) (list on-path)))
          ;; ROOT/.venv beats PATH.
          (let ((venv (triage-test-make-exe root ".venv/bin/triage")))
            (should (equal (triage--find-executable root) (list venv)))
            ;; $UV_PROJECT_ENVIRONMENT beats .venv.
            (let ((uv (triage-test-make-exe uv-env "bin/triage")))
              (should (equal (triage--find-executable root) (list uv))))))))))

(ert-deftest triage-test-executable-option-wins ()
  (let ((triage-executable "/opt/triage/bin/triage"))
    (should (equal (triage--find-executable "/tmp/") '("/opt/triage/bin/triage"))))
  (let ((triage-executable '("uv" "run" "--frozen" "triage")))
    (should (equal (triage--find-executable "/tmp/") '("uv" "run" "--frozen" "triage")))))

(ert-deftest triage-test-call-json-parses-the-payload ()
  (triage-test-in-project
    (triage-test-with-stub (triage-test-fixture-responder "status-full")
      (let (got)
        (triage--call-json '("status" "--json" "--full") (lambda (p) (setq got p)))
        (should (equal (car triage-test--calls) '("triage" "status" "--json" "--full")))
        (should (equal (alist-get 'contract got) 1))
        (should (eq (triage--get got 'database 'connected) t))
        ;; JSON null reads as nil.
        (should (null (alist-get 'archived_at (car (alist-get 'experiments got)))))))))

(ert-deftest triage-test-nonzero-exit-goes-to-the-error-buffer ()
  (when (get-buffer triage-errors-buffer) (kill-buffer triage-errors-buffer))
  (triage-test-in-project
    (triage-test-with-stub (lambda (_c) (list 2 "" "INFO starting\nError: No such option: --full\n"))
      (let (called err)
        (triage--call-json '("status" "--json" "--full")
                           (lambda (_p) (setq called t))
                           :errback (lambda (m) (setq err m)))
        (should-not called)
        (should (equal err "Error: No such option: --full"))
        (with-current-buffer triage-errors-buffer
          (should (string-match-p "triage status --json --full (exit 2)" (buffer-string)))
          (should (string-match-p "INFO starting" (buffer-string))))))))

(ert-deftest triage-test-output-that-is-not-json-is-the-error ()
  ;; `triage query --json' prints a failed statement's error on stdout, exit 0.
  (triage-test-in-project
    (triage-test-with-stub (lambda (_c) (list 0 "error relation \"nope\" does not exist\n" ""))
      (let (called err)
        (triage--call-json '("query" "--json" "select * from nope")
                           (lambda (_p) (setq called t))
                           :errback (lambda (m) (setq err m)))
        (should-not called)
        (should (equal err "error relation \"nope\" does not exist"))))))

(ert-deftest triage-test-contract-newer-than-the-elisp ()
  (triage-test-in-project
    (triage-test-with-stub (lambda (_c) (list 0 "{\"contract\": 2, \"project\": \"x\"}" ""))
      (let (called err)
        (triage--call-json '("status" "--json" "--full") (lambda (_p) (setq called t))
                           :errback (lambda (m) (setq err m)) :require-contract "status --full")
        (should-not called)
        (should (string-match-p "triage-pg is newer than triage.el.*update triage.el" err))))))

(ert-deftest triage-test-contract-missing-from-an-old-cli ()
  (triage-test-in-project
    (triage-test-with-stub (lambda (_c) (list 0 "{\"project\": \"x\", \"database\": {}}" ""))
      (let (called err)
        (triage--call-json '("status" "--json" "--full") (lambda (_p) (setq called t))
                           :errback (lambda (m) (setq err m)) :require-contract "status --full")
        (should-not called)
        (should (equal err (format "triage-pg >= %s needed (status --full missing)"
                                   triage--min-version)))))))

(ert-deftest triage-test-contract-equal-passes ()
  (should-not (triage--contract-problem (triage-test-payload "status-full") "status --full"))
  ;; A payload with no contract is fine when none is required (runs list).
  (should-not (triage--contract-problem (triage-test-payload "runs-list") nil)))

(ert-deftest triage-test-run-in-buffer-starts-one-process-per-name ()
  (triage-test-in-project
    (let* ((triage-executable '("triage"))
           (started nil)
           (triage--process-starter (lambda (&rest args) (push args started) nil)))
      (let ((buffer (triage--run-in-buffer "gc" '("gc"))))
        (unwind-protect
            (progn
              (should (equal (buffer-name buffer) "*triage: gc*"))
              (should (equal (plist-get (car started) :command) '("triage" "gc")))
              (with-current-buffer buffer
                (should (derived-mode-p 'triage-process-mode))
                (should (equal triage--root root)))
              (should (eq (triage--run-in-buffer "gc" '("gc" "--delete")) buffer))
              (should (= (length started) 2)))
          (kill-buffer buffer))))))

;;;; Phase 5: the status buffer

(defun triage-test-sections ()
  "Return (TYPE . VALUE) for every section of the current buffer, in order."
  (let (out)
    (magit-map-sections (lambda (s) (push (cons (oref s type) (oref s value)) out)))
    (nreverse out)))

(defun triage-test-section (type value)
  "Return the section of TYPE and VALUE in the current buffer."
  (let (found)
    (magit-map-sections (lambda (s) (when (and (eq (oref s type) type)
                                               (equal (oref s value) value))
                                      (setq found s))))
    found))

(defun triage-test-children (section)
  "Return (TYPE . VALUE) of the children of SECTION."
  (mapcar (lambda (s) (cons (oref s type) (oref s value))) (oref section children)))

(defmacro triage-test-with-status (payload &rest body)
  "Run BODY in a status buffer drawn from PAYLOAD, without any call."
  (declare (indent 1))
  `(triage-test-in-project
     (let ((buf (generate-new-buffer "*triage-test-status*"))
           (triage-executable '("triage"))
           (state (triage--capture)))
       (unwind-protect
           (with-current-buffer buf
             (triage-status-mode)
             (triage--restore state)
             (setq triage-status--payload ,payload)
             (triage-status--render)
             ,@body)
         (kill-buffer buf)))))

(defun triage-test-join-payload ()
  "Return status-full.json with experiments and configs for every join case."
  (let ((p (copy-tree (triage-test-payload "status-full")))
        (dash (car (alist-get 'experiments (triage-test-payload "status-full")))))
    (setf (alist-get 'experiments p)
          (list dash
                `((experiment_hash . "h-db") (name . "stored-only") (problem_type . "regression")
                  (n_runs . 1) (last_status . "failed") (archived_at))
                `((experiment_hash . "h-arch") (name . "old-one") (problem_type . "classification")
                  (n_runs . 3) (last_status . "completed") (archived_at . "2026-10-01T00:00:00+00:00"))))
    (setf (alist-get 'configs p)
          (list '((path . "experiments/a.yaml") (experiment_hash . "exp-dash") (name)
                  (problem_type . "classification") (error))
                '((path . "experiments/a-copy.yaml") (experiment_hash . "exp-dash") (name)
                  (problem_type . "classification") (error))
                '((path . "experiments/new.yaml") (experiment_hash . "h-new") (name . "fresh")
                  (problem_type . "classification") (error))
                '((path . "experiments/old.yaml") (experiment_hash . "h-arch") (name)
                  (problem_type . "classification") (error))
                '((path . "experiments/broken.yaml") (experiment_hash) (name) (problem_type)
                  (error . "mapping values are not allowed here"))))
    p))

(ert-deftest triage-test-status-join-four-cases ()
  (let* ((triage-show-archived nil)
         (join (triage-status--experiment-rows (triage-test-join-payload)))
         (rows (car join)))
    (should (equal (mapcar (lambda (r) (plist-get r :hash)) rows) '("exp-dash" "h-db" "h-new")))
    ;; Several files under one hash.
    (should (= (length (plist-get (nth 0 rows) :configs)) 2))
    ;; A database row with no file.
    (should (null (plist-get (nth 1 rows) :configs)))
    ;; A config with no database row.
    (should (null (plist-get (nth 2 rows) :experiment)))
    ;; A config that failed to parse.
    (should (equal (mapcar (lambda (c) (alist-get 'path c)) (cdr join))
                   '("experiments/broken.yaml")))))

(ert-deftest triage-test-status-renders-the-join ()
  (triage-test-with-status (triage-test-join-payload)
    (let ((text (buffer-string)))
      (should (string-match-p "Experiments (3)" text))
      (should (string-match-p "Churn baseline .*classification .*2 runs .*completed .*2 config files" text))
      (should (string-match-p "stored-only .*regression .*1 run .*failed .*(no config file)" text))
      (should (string-match-p "fresh .*classification .*— .*not run .*experiments/new.yaml" text))
      (should (string-match-p "experiments/broken.yaml .*mapping values are not allowed" text))
      (should-not (string-match-p "old-one" text)))
    (should (equal (triage-test-children (triage-test-section 'triage-experiment "exp-dash"))
                   '((triage-config . "experiments/a.yaml")
                     (triage-config . "experiments/a-copy.yaml"))))
    (should (equal (mapcar #'cdr (seq-filter (lambda (s) (eq (car s) 'triage-experiment))
                                             (triage-test-sections)))
                   '("exp-dash" "h-db" "h-new")))
    ;; The menus read the single config of an Experiment from the section data.
    (goto-char (oref (triage-test-section 'triage-experiment "h-new") start))
    (should (equal (triage--at-point :config) "experiments/new.yaml"))
    (should (equal (triage--at-point :hash) "h-new"))))

(ert-deftest triage-test-status-archived-hidden-and-shown ()
  (triage-test-with-status (triage-test-join-payload)
    (let ((triage-show-archived nil))
      (triage-status--render)
      (should-not (triage-test-section 'triage-experiment "h-arch"))
      (triage-test-with-stub (lambda (_c) (error "No call expected"))
        (triage-status-toggle-archived)
        (should (null triage-test--calls)))
      (should (triage-test-section 'triage-experiment "h-arch"))
      (should (string-match-p "old-one .*experiments/old.yaml  archived" (buffer-string))))))

(ert-deftest triage-test-status-offline-header ()
  (triage-test-with-status (triage-test-payload "status-full-offline")
    (let ((text (buffer-string)))
      (should (string-match-p "DB: connection failed: connection to server" text))
      (should (eq (get-text-property (1+ (string-match "DB: connection failed" text)) 'font-lock-face)
                  'triage-danger))
      ;; The configs still fill the Experiments section, with no claim that they never ran.
      (should (string-match-p "Experiments (1)" text))
      (should (string-match-p "b9e38fd8f366 .*classification .*— .*— .*inspections.yaml" text))
      (should-not (string-match-p "not run" text)))))

(ert-deftest triage-test-status-header-online ()
  (triage-test-with-status (triage-test-payload "status-full")
    (let ((text (buffer-string)))
      (should (string-match-p "Project:  tests .*DB: connected · pg 18 · 0028_subset_threshold_curve · 9574 kB" text))
      (should (string-match-p "triage:   triage (1.5.0)" text))
      (should (string-match-p "Recent runs (2)" text))
      (should (string-match-p "Sources (1)" text))
      (should (string-match-p "Tables (" text)))
    ;; Sources and Tables start folded.
    (should (oref (triage-test-section 'triage-sources nil) hidden))
    (should (oref (triage-test-section 'triage-relations nil) hidden))))

(ert-deftest triage-test-status-one-call-per-refresh ()
  (triage-test-in-project
    (triage-test-write default-directory "database.yaml" "")
    (triage-clear-caches)
    (triage-test-with-stub (triage-test-fixture-responder "status-full")
      (let ((buf (triage-status default-directory)))
        (unwind-protect
            (with-current-buffer buf
              (should (= (length triage-test--calls) 1))
              (should (equal (car triage-test--calls) '("triage" "status" "--json" "--full")))
              (triage-status-refresh)
              (should (= (length triage-test--calls) 2))
              ;; exp-dash from the database, plus inspections.yaml, which has not run.
              (should (string-match-p "Experiments (2)" (buffer-string))))
          (kill-buffer buf))))))

(ert-deftest triage-test-status-returns-before-the-call-completes ()
  (triage-test-in-project
    (triage-test-write default-directory "database.yaml" "")
    (triage-clear-caches)
    (let ((triage-test--defer t))
      (triage-test-with-stub (triage-test-fixture-responder "status-full")
        (let ((buf (triage-status default-directory)))
          (unwind-protect
              (with-current-buffer buf
                ;; The command returned; the call is still out.
                (should triage-status--refreshing)
                (should (string-match-p "Loading…" (buffer-string)))
                ;; A second g while it is out starts nothing.
                (triage-status-refresh)
                (should (= (length triage-test--calls) 1))
                (triage-test-deliver)
                (should-not triage-status--refreshing)
                (should (string-match-p "Churn baseline" (buffer-string))))
            (kill-buffer buf)))))))

(ert-deftest triage-test-status-folding-and-point-survive-a-refresh ()
  (triage-test-with-status (triage-test-join-payload)
    (magit-section-show (triage-test-section 'triage-relations nil))
    (magit-section-hide (triage-test-section 'triage-experiments nil))
    (goto-char (oref (triage-test-section 'triage-runs nil) start))
    (triage-status--render)
    (should-not (oref (triage-test-section 'triage-relations nil) hidden))
    (should (oref (triage-test-section 'triage-experiments nil) hidden))
    (should (oref (triage-test-section 'triage-sources nil) hidden))
    (should (eq (oref (magit-current-section) type) 'triage-runs))))

(ert-deftest triage-test-status-error-shows-in-the-header ()
  (triage-test-in-project
    (triage-test-write default-directory "database.yaml" "")
    (triage-clear-caches)
    (triage-test-with-stub (lambda (_c) (list 0 "{\"contract\": 7}" ""))
      (let ((buf (triage-status default-directory)))
        (unwind-protect
            (with-current-buffer buf
              (should (string-match-p "Error: +triage-pg is newer than triage.el" (buffer-string))))
          (kill-buffer buf))))))

;;;; Phase 6: object views

(defconst triage-test--group-json
  "{\"group_avg\": 0.81, \"models\": [{\"model_id\": 4, \"train_end_time\": \"2014-01-01\", \"train_duration_ms\": null, \"value_mean\": 0.8}, {\"model_id\": 5, \"train_end_time\": \"2014-07-01\", \"train_duration_ms\": 12, \"value_mean\": 0.82}]}"
  "What triage models HASH --group G --json prints; no fixture covers it.")

(defconst triage-test--audition-json
  "{\"experiment_hash\": \"exp-dash\", \"metric\": \"auc_roc\", \"parameter\": \"\", \"rule\": \"best_average_value\", \"window\": {\"from\": null, \"to\": null}, \"ranking\": [{\"model_group_id\": 2, \"avg_value\": 0.81, \"max_regret\": 0.09}], \"strategies\": [{\"rule\": \"best_average_value\", \"model_group_id\": 2}], \"selected\": {\"audition_group\": 2, \"audition_model\": 6}}"
  "What triage audition HASH --json prints, in the shape DirtyDuck gave; no fixture covers it.")

(defun triage-test-router (command)
  "Answer COMMAND with the fixture of the verb it calls."
  (let ((args (cdr command)))
    (list 0
          (pcase args
            (`("status" . ,_) (triage-test-fixture "status-full"))
            (`("experiment" "show" ,target . ,_)
             (triage-test-fixture (if (string-suffix-p ".yaml" target)
                                      "experiment-show-file" "experiment-show-hash")))
            (`("runs" "show" . ,_) (triage-test-fixture "runs-show"))
            (`("runs" "list" . ,_) (triage-test-fixture "runs-list"))
            (`("model" "show" . ,_) (triage-test-fixture "model-show"))
            ((and `("models" . ,_) (guard (member "--group" args))) triage-test--group-json)
            (`("models" . ,_) (triage-test-fixture "models"))
            (`("leaderboard" . ,_) (triage-test-fixture "leaderboard"))
            (`("query" . ,_) (triage-test-fixture "query"))
            (`("actions" "list" . ,_) (triage-test-fixture "actions-list"))
            (`("plot" . ,_) "")
            (`("audition" . ,_) triage-test--audition-json)
            (_ (error "No fixture for %S" args)))
          "")))

(defmacro triage-test-with-views (&rest body)
  "Run BODY in a project with the router stub, killing the view buffers after."
  (declare (indent 0))
  `(triage-test-in-project
     (triage-test-with-stub #'triage-test-router
       (let ((before (buffer-list)))
         (unwind-protect (progn ,@body)
           (dolist (b (buffer-list))
             (unless (memq b before) (kill-buffer b))))))))

(ert-deftest triage-test-experiment-view-from-a-file ()
  (triage-test-with-views
    (triage-test-write root "inspections.yaml" "temporal_config: {}\nlabel_config: {}\n")
    (with-current-buffer (triage-experiment-show "inspections.yaml")
      (should (equal (car triage-test--calls)
                     '("triage" "experiment" "show" "inspections.yaml" "--json")))
      (let ((text (buffer-string)))
        (should (string-match-p "source: +inspections.yaml" text))
        (should (string-match-p "Validation: valid" text))
        (should (string-match-p "models to train: +32" text))
        (should (string-match-p "label timespans: +6 months" text))
        (should (string-match-p "Subsets (1)" text))
        (should (string-match-p "History\n +not run yet" text)))
      (should (oref (triage-test-section 'triage-validation nil) hidden))
      (should (eq (get-text-property (1+ (string-match "32" (buffer-string))) 'font-lock-face)
                  'bold))
      (should (equal (plist-get triage--context :config) "inspections.yaml"))
      (should (equal (triage--at-point :hash)
                     "b9e38fd8f3669d9451f4deaa3a13f33130c5e4fd1f95dbc1c1ae3420816c7725")))))

(ert-deftest triage-test-experiment-view-from-a-hash ()
  (triage-test-with-views
    (with-current-buffer (triage-experiment-show "exp-dash")
      (should (equal (car triage-test--calls) '("triage" "experiment" "show" "exp-dash" "--json")))
      (let ((text (buffer-string)))
        (should (string-match-p "Experiment Churn baseline" text))
        (should (string-match-p "source: +stored config" text))
        (should (string-match-p "Runs (2)" text))
        (should (string-match-p "Model groups (3) on auc_roc" text))
        (should (string-match-p "DecisionTreeClassifier +3 +0.8100 ± 0.0100 +0.0900" text))
        (should (string-match-p "Leaderboard (top 10)" text)))
      ;; RET on a model group opens its members; on a leaderboard row, the model.
      (goto-char (oref (triage-test-section 'triage-model-group 2) start))
      (with-current-buffer (triage-visit)
        (should (equal (car triage-test--calls)
                       '("triage" "models" "exp-dash" "--group" "2" "--json"))))
      (goto-char (oref (triage-test-section 'triage-model 3) start))
      (should (equal (triage--at-point :model) 3))
      (should (equal (triage--at-point :group) 1))
      (triage-visit)
      (should (equal (car triage-test--calls) '("triage" "model" "show" "3" "--json"))))))

(ert-deftest triage-test-experiment-view-offline-history ()
  (triage-test-in-project
    (let ((payload (copy-tree (triage-test-payload "experiment-show-file"))))
      (setf (alist-get 'database payload)
            '((connected . :false) (detail . "connection failed: port 1")))
      (let ((buf (generate-new-buffer "*triage-test-experiment*"))
            (state (triage--capture)))
        (unwind-protect
            (with-current-buffer buf
              (triage-experiment-mode)
              (triage--restore state)
              (setq triage-object--renderer #'triage-experiment--insert
                    triage--payload payload)
              (triage-object--render)
              (should (string-match-p "History\n +database unreachable: connection failed: port 1"
                                      (buffer-string)))
              (should-not (string-match-p "not run yet" (buffer-string))))
          (kill-buffer buf))))))

(ert-deftest triage-test-experiment-edit-shows-the-stored-config ()
  (triage-test-with-views
    (with-current-buffer (triage-experiment-show "exp-dash")
      (let ((config (triage-experiment-edit)))
        (with-current-buffer (if (windowp config) (window-buffer config) config)
          (should buffer-read-only)
          (should (string-prefix-p "sources:\n- name: ontology_entities" (buffer-string))))))))

(ert-deftest triage-test-run-view-and-tail ()
  (triage-test-with-views
    (let* ((started nil)
           (triage--process-starter (lambda (&rest args) (push args started) nil)))
      (with-current-buffer (triage-run-show "00000000-0000-4000-8000-000000000001")
        (should (equal (car triage-test--calls)
                       '("triage" "runs" "show" "00000000-0000-4000-8000-000000000001" "--json")))
        (let ((text (buffer-string)))
          (should (string-match-p "state: +succeeded" text))
          (should (string-match-p "predictions +1 / 9 " text))
          (should (string-match-p "experiment: +exp-dash" text)))
        (should (equal (triage--at-point :hash) "exp-dash"))
        (triage-run-tail)
        (should (equal (plist-get (car started) :command)
                       '("triage" "runs" "tail" "00000000-0000-4000-8000-000000000001")))))))

(ert-deftest triage-test-model-group-list ()
  (triage-test-with-views
    (with-current-buffer (triage-model-group-show "exp-dash" 2)
      (should (equal (car triage-test--calls)
                     '("triage" "models" "exp-dash" "--group" "2" "--json")))
      (should (string-match-p "group average 0.8100" header-line-format))
      (should (= (length tabulated-list-entries) 2))
      ;; The column names take the first line; the first row follows.
      (goto-char (point-min))
      (while (and (not (tabulated-list-get-id)) (not (eobp))) (forward-line))
      (should (equal (triage--at-point :model) 4))
      (should (equal (triage--at-point :hash) "exp-dash"))
      (triage-table-visit)
      (should (equal (car triage-test--calls) '("triage" "model" "show" "4" "--json"))))))

(ert-deftest triage-test-model-view ()
  (triage-test-with-views
    (with-current-buffer (triage-model-show 3)
      (let ((text (buffer-string)))
        (should (string-match-p "Model 3" text))
        (should (string-match-p "model_type: +sklearn.tree.DecisionTreeClassifier" text))
        (should (string-match-p "Windowed metrics (2)" text))
        (should (string-match-p "auc_roc .*0.9000" text))
        (should (string-match-p "Feature importances (0)" text)))
      (should (equal (triage--at-point :group) 1)))))

(ert-deftest triage-test-table-view-quotes-the-relation ()
  (triage-test-with-views
    (let ((triage-table-limit 50))
      (with-current-buffer (triage-table-show "odd\"name")
        (should (equal (car triage-test--calls)
                       '("triage" "query" "--json"
                         "select * from triage.\"odd\"\"name\" limit 50")))
        (should (= (length tabulated-list-entries)
                   (length (triage-test-payload "query"))))
        (pcase-let ((`(,name ,width ,sort . ,props) (aref tabulated-list-format 0)))
          (should (equal name "model_group_id"))
          (should (= width 14))
          (should (functionp sort))
          (should (plist-get props :right-align)))
        (should (equal header-line-format
                       "triage query --json select * from triage.\"odd\"\"name\" limit 50"))))))

(ert-deftest triage-test-query-error-shows-in-the-header ()
  (triage-test-in-project
    (triage-test-with-stub (lambda (_c) (list 0 "error syntax error at or near \"selec\"\n" ""))
      (with-current-buffer (triage-query "selec 1")
        (unwind-protect
            (progn
              (should (equal header-line-format "error syntax error at or near \"selec\""))
              (should (null tabulated-list-entries)))
          (kill-buffer))))))

(ert-deftest triage-test-status-ret-opens-the-experiment ()
  (triage-test-with-views
    (triage-test-write root "database.yaml" "")
    (triage-clear-caches)
    (with-current-buffer (triage-status root)
      ;; inspections.yaml has a file: RET reads the file.
      (goto-char (oref (triage-test-section
                        'triage-experiment
                        "b9e38fd8f3669d9451f4deaa3a13f33130c5e4fd1f95dbc1c1ae3420816c7725")
                       start))
      (triage-visit))
    (should (equal (car triage-test--calls)
                   '("triage" "experiment" "show" "inspections.yaml" "--json")))
    ;; exp-dash has no file here: RET reads the stored config by hash.
    (with-current-buffer (get-buffer (format "*triage: %s*" (triage--project-name root)))
      (goto-char (oref (triage-test-section 'triage-experiment "exp-dash") start))
      (triage-visit))
    (should (equal (car triage-test--calls) '("triage" "experiment" "show" "exp-dash" "--json")))))

;;;; Phase 7: transient menus

(defvar triage-test--started nil
  "Keyword arguments the stubbed process-buffer starter received, newest first.")

(defmacro triage-test-with-menus (&rest body)
  "Run BODY with both process starters stubbed and the router answering."
  (declare (indent 0))
  `(triage-test-with-views
     (let* ((triage-test--started nil)
            (triage--process-starter (lambda (&rest args) (push args triage-test--started) nil)))
       ,@body)))

(defun triage-test-started ()
  "Return the command of the newest process-buffer start, without the executable."
  (cdr (plist-get (car triage-test--started) :command)))

(defun triage-test-buffer-on-exit (name code)
  "Run the exit function of the process buffer *triage: NAME* with CODE."
  (with-current-buffer (format "*triage: %s*" name)
    (funcall triage--process-on-exit code)))

(ert-deftest triage-test-menu-run-local-and-cloud ()
  (triage-test-with-menus
    (triage-run-config "experiments/a.yaml" '("--profile=local" "--random-seed=3"))
    (should (equal (triage-test-started)
                   '("run" "experiments/a.yaml" "--profile=local" "--random-seed=3")))
    (triage-run-config "experiments/a.yaml" '("--profile=cloud" "--cache-policy=logical"))
    (should (equal (triage-test-started)
                   '("run" "experiments/a.yaml" "--profile=cloud" "--cache-policy=logical")))
    (let (said)
      (cl-letf (((symbol-function 'message)
                 (lambda (fmt &rest args) (push (apply #'format fmt args) said))))
        (triage-test-buffer-on-exit "run a.yaml" 0))
      (should (member "triage: the run was submitted to AWS Batch; it runs there" said)))))

(defun triage-test-leakage-responder (code)
  "Return a responder for check leakage that exits with CODE.
Exit 1 writes the leakage-report fixture where --json points."
  (lambda (command)
    (if (not (equal (seq-take (cdr command) 2) '("check" "leakage")))
        (triage-test-router command)
      (when (= code 1)
        (with-temp-file (cadr (member "--json" command))
          (insert (triage-test-fixture "leakage-report"))))
      (list code (if (= code 1) "leakage found (exit 1).\n" "")
            (if (= code 2) "ERROR could not reach the database\n" "")))))

(ert-deftest triage-test-menu-check-exit-0 ()
  (triage-test-in-project
    (triage-test-with-stub (triage-test-leakage-responder 0)
      (let (said)
        (cl-letf (((symbol-function 'message)
                   (lambda (fmt &rest args) (push (apply #'format fmt args) said))))
          (triage-check-leakage "a.yaml" '("--dates=2014-01-01")))
        (let ((command (cdar triage-test--calls)))
          (should (equal (seq-take command 4) '("check" "leakage" "a.yaml" "--json")))
          (should (equal (nthcdr 5 command) '("--dates=2014-01-01")))
          (should-not (file-exists-p (nth 4 command))))
        (should (member "triage: leakage check of a.yaml found no differences" said))))))

(ert-deftest triage-test-menu-check-exit-1-opens-the-report ()
  (triage-test-in-project
    (triage-test-with-stub (triage-test-leakage-responder 1)
      (triage-check-leakage "a.yaml" nil)
      (let ((report (nth 5 (car triage-test--calls))))
        (should-not (file-exists-p report)))
      (with-current-buffer "*triage: leakage a.yaml*"
        (unwind-protect
            (let ((text (buffer-string)))
              (should (string-match-p "leakage found: +true" text))
              (should (string-match-p "Findings (2)" text))
              (should (string-match-p "labels +2013-07-01 .*-1 .*rows known on or after 2014-01-01" text)))
          (kill-buffer))))))

(ert-deftest triage-test-menu-check-exit-2-reports-the-error ()
  (when (get-buffer triage-errors-buffer) (kill-buffer triage-errors-buffer))
  (triage-test-in-project
    (triage-test-with-stub (triage-test-leakage-responder 2)
      (let ((inhibit-message t))
        (triage-check-leakage "a.yaml" nil))
      (should-not (get-buffer "*triage: leakage a.yaml*"))
      (with-current-buffer triage-errors-buffer
        (should (string-match-p "check leakage a.yaml --json .* (exit 2)" (buffer-string)))
        (should (string-match-p "could not reach the database" (buffer-string)))))))

(ert-deftest triage-test-menu-analyze-with-plot ()
  (triage-test-with-menus
    (triage-analyze-config "a.yaml" '("--estimate" "--plot" "--estimate-dates=2"))
    (let ((command (triage-test-started)))
      (should (equal (seq-take command 4) '("analyze-config" "a.yaml" "--estimate" "--estimate-dates=2")))
      (should (equal (nth 4 command) "--plot"))
      (should (string-suffix-p ".svg" (nth 5 command)))
      (should (= (length command) 6)))))

(ert-deftest triage-test-menu-leaderboard-refresh-and-audition ()
  (triage-test-with-menus
    (triage-leaderboard-refresh "exp-dash" '("--metric=auc_roc" "--windowed" "--rule=x"))
    (should (member '("triage" "leaderboard" "exp-dash" "--json" "--refresh"
                      "--metric=auc_roc" "--windowed")
                    triage-test--calls))
    (triage-audition "exp-dash" '("--metric=auc_roc" "--windowed"))
    (should (equal (car triage-test--calls)
                   '("triage" "audition" "exp-dash" "--json" "--metric=auc_roc")))
    (with-current-buffer "*triage: audition exp-dash*"
      (should (string-match-p "ranking (1)" (buffer-string)))
      (should (string-match-p "  audition_model: +6" (buffer-string)))
      ;; A ranking row is its model group: RET opens the members.
      (goto-char (oref (triage-test-section 'triage-model-group 2) start))
      (should (equal (triage--at-point :hash) "exp-dash"))
      (triage-visit)
      (should (equal (car triage-test--calls)
                     '("triage" "models" "exp-dash" "--group" "2" "--json"))))))

(ert-deftest triage-test-menu-score ()
  (triage-test-with-menus
    (triage-score-model 3 "2026-01-01" '("--cohort-query=open.sql"))
    (should (equal (triage-test-started) '("score" "3" "2026-01-01" "--cohort-query=open.sql")))
    (triage-score-model "3" "" nil)
    (should (equal (triage-test-started) '("score" "3")))))

(ert-deftest triage-test-menu-plot-passes-only-its-flags ()
  (triage-test-with-menus
    (let ((inhibit-message t))
      (triage-plot-curve '(3) '("--date=2015-01-01" "--kind=fp" "--top=5" "--compare=4")))
    (let ((command (cdar triage-test--calls)))
      (should (equal (seq-take command 5)
                     '("plot" "curve" "3" "--date=2015-01-01" "--compare=4")))
      (should (equal (nth 5 command) "-o"))
      (should (string-suffix-p ".svg" (nth 6 command))))))

(ert-deftest triage-test-menu-plot-reads-a-required-cut ()
  (triage-test-with-menus
    (cl-letf (((symbol-function 'read-string) (lambda (&rest _) "100_abs")))
      (let ((inhibit-message t))
        (triage-plot-leaves '(2) '("--date=2015-01-01"))
        (should (equal (seq-take (cdar triage-test--calls) 4)
                       '("plot" "leaves" "2" "--parameter=100_abs")))
        (triage-plot-overlap '(2 "3") '("--parameter=10_pct"))
        (should (equal (seq-take (cdar triage-test--calls) 5)
                       '("plot" "overlap" "2" "3" "--parameter=10_pct")))))))

(ert-deftest triage-test-menu-archive-declined-starts-nothing ()
  (triage-test-with-menus
    (cl-letf (((symbol-function 'yes-or-no-p) (lambda (_) nil)))
      (triage-archive-experiment "exp-dash"))
    (should (null triage-test--started))
    (cl-letf (((symbol-function 'yes-or-no-p) (lambda (_) t)))
      (triage-archive-experiment "exp-dash"))
    (should (equal (triage-test-started) '("archive" "exp-dash")))))

(ert-deftest triage-test-menu-gc-dry-run-and-delete ()
  (triage-test-with-menus
    (triage-gc-dry-run '("--min-age=7" "--purge"))
    (should (equal (triage-test-started) '("gc" "--min-age=7")))
    (cl-letf (((symbol-function 'yes-or-no-p) (lambda (_) nil)))
      (triage-gc-delete '("--purge")))
    (should (= (length triage-test--started) 1))
    (cl-letf (((symbol-function 'yes-or-no-p) (lambda (_) t)))
      (triage-gc-delete '("--min-age=7" "--purge")))
    (should (equal (triage-test-started) '("gc" "--delete" "--min-age=7" "--purge")))))

(ert-deftest triage-test-menu-serve-opens-the-browser-once ()
  (triage-test-with-menus
    (triage-serve '("--port=8123"))
    (should (equal (triage-test-started) '("serve" "--port=8123")))
    (with-current-buffer "*triage: serve*"
      (let ((watch triage--process-on-match)
            (line "INFO:     Uvicorn running on http://127.0.0.1:8123 (Press CTRL+C to quit)"))
        (should (eq (cdr watch) #'browse-url))
        (should (string-match (car watch) line))
        (should (equal (match-string 1 line) "http://127.0.0.1:8123"))))))

(ert-deftest triage-test-menu-inapt-without-a-target ()
  (triage-test-in-project
    ;; No status buffer, nothing at point: nothing to prompt from.
    (should (triage-transient--no-hash-p))
    (should (triage-transient--no-model-p))
    (should (triage-transient--no-plot-target-p)))
  (triage-test-with-views
    (with-current-buffer (triage-experiment-show "exp-dash")
      (should-not (triage-transient--no-hash-p))
      ;; The leaderboard rows of the view offer models to a prompt.
      (should-not (triage-transient--no-model-p))
      (should-not (triage-transient--no-group-p)))))

(ert-deftest triage-test-menu-verbs-exist ()
  ;; Run every suffix once, then look up each verb in actions list --json,
  ;; so a renamed CLI verb fails here.
  (triage-test-with-menus
    (cl-letf (((symbol-function 'yes-or-no-p) (lambda (_) t))
              ((symbol-function 'read-string) (lambda (&rest _) "100_abs")))
      (let ((inhibit-message t))
        (triage-run-config "a.yaml" nil)
        (triage-analyze-config "a.yaml" nil)
        (triage-leaderboard "exp-dash" nil)
        (triage-audition "exp-dash" nil)
        (triage-model-groups "exp-dash" nil)
        (triage-score-model 3 "" nil)
        (dolist (sub triage-plot-subcommands)
          (funcall (intern (format "triage-plot-%s" (car sub)))
                   (if (eq (cadr sub) :groups) '(1 2) '(1)) nil))
        (triage-archive-experiment "exp-dash")
        (triage-gc-dry-run nil)
        (triage-db-upgrade)
        (triage-serve nil)
        (with-current-buffer (triage-run-show "00000000-0000-4000-8000-000000000001")
          (triage-run-tail))
        (triage-model-show 3)
        (triage-experiment-show "exp-dash")
        (triage-query "select 1")))
    (triage-test-with-stub (triage-test-leakage-responder 0)
      (let ((inhibit-message t)) (triage-check-leakage "a.yaml" nil))
      (push (car triage-test--calls) triage-test--started))
    (let* ((names (mapcar (lambda (a) (alist-get 'name a)) (triage-test-payload "actions-list")))
           (commands (append (mapcar #'cdr triage-test--calls)
                             (mapcar (lambda (s) (cdr (if (plist-member s :command)
                                                          (plist-get s :command) s)))
                                     triage-test--started))))
      (should (> (length commands) 20))
      (dolist (command commands)
        (should (or (member (format "triage %s %s" (car command) (cadr command)) names)
                    (member (format "triage %s" (car command)) names)))))))

(provide 'triage-test)

;;; triage-test.el ends here
