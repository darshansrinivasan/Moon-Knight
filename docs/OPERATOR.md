# Operating Pylon QC

Day-to-day scoring and human sign-off. Configuration still lives in Admin; this
page is what you do after the app is running.

## Score a day

1. Open **Dashboard** and click a day. The first click fetches that day's
   tickets from Pylon and runs the deterministic R-checks (R1–R8).
2. Click **▶ Run QC**. Gemini grades A1–A5 on Vertex AI. Cards refresh as
   batches land; the header shows an estimated cost when Vertex reports token
   usage. If usage is missing, the header says `usage not reported` instead of
   a fake `$0`.
3. Re-run QC on the same day if tickets moved. AI grades in `ai_checks` are
   overwritten by the new run. Human sign-off is a separate record and is not
   wiped.

**Automated daily run** lives on **Runs**: enable the schedule, pick time and
timezone, and optionally **Run now**. That pipeline is fetch → QC → Slack. It
does not accept tickets for you.

## Cost

Cost is an estimate from the model price table and the tokens Vertex returns
on each call. The latest run for the selected day is what the dashboard header
shows. Full history (tokens, estimate, grade stability) is on **Runs**.

**Quota project** (Admin → Vertex AI) is the Google Cloud project billed for
those calls. Leave it as **Same as Vertex project** unless you bill another
project. The picker lists the same projects as the Vertex project field.

## Who may review

There is no extra `reviewer` role.

- **Admins** (People → role Admin, plus `QC_ADMIN_EMAILS`) can sign off every
  ticket.
- **Coverage reviewers** are people picked from the Slack directory. They sign
  in with Google; the match is their Slack `profile.email`, which must be on
  the same domain as login (`@spotdraft.com`).

The Slack bot token needs `chat:write` (reports), `users:read`, and
`users:read.email` (reviewer picker). Without the email scope the picker will
be empty even though Rules can still search names.

Unassigned tickets are admin-only unless a coverage includes **Unassigned**.

## Set up region coverage

Admin → **Review coverage**. Example:

- Coverage `APAC` → reviewer A → assignees in APAC
- Coverage `NAM` → reviewer B → assignees in NAM

One person can own several coverages. Assignees appear after those people have
shown up on a fetched ticket. Save the group; the reviewer can sign off as
soon as they next open the dashboard.

## Accept QC / Pass / Fail

Sign-off is **per ticket**, not per day. Expand a card:

- **Accept QC** — keep the AI overall and record who/when. Disabled when AI
  graded `Needs Review` or the ticket has not been scored; pick Pass or Fail
  instead.
- **Pass** / **Fail** — override (or confirm) the overall grade. The AI
  overall is stored separately and is never overwritten.

The dashboard list, status pills, Unreviewed filter, day totals, and CSV use
the **effective** grade: latest sign-off if present, otherwise the AI overall.
Calendar heat and Slack reports still use the AI overall for this version.

**Unreviewed** means QC has run and nobody has signed off yet.

## Slack

Scheduled Slack summaries are unchanged: they report the AI grade, not the
human decision. Do not expect a channel post when someone clicks Accept QC.
