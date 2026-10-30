# Sourced by the laptop-side tools. Loads your private settings from $AGENT_SETUP_ENV
# (default ~/.config/claude-agent-setup/env), a bash fragment such as:
#   AGENT_RULES_HOST=my-vps                  # ssh host the installers write to (required by them)
#   AGENT_OVERLAY_DIR=~/private/agent-overlay   # your routine confs, job prompts, extra scripts
#   NOTIFY_ENV_SRC=/srv/app/.env             # VPS file holding TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
#   GH_AUTHOR=your-github-login              # routine-verdict: whose PRs count as routine PRs
# Variables already set in the environment win over the file.
_agent_env_file="${AGENT_SETUP_ENV:-$HOME/.config/claude-agent-setup/env}"
if [ -r "$_agent_env_file" ]; then
  _agent_env_keep=$(export -p)
  # shellcheck disable=SC1090
  set -a; . "$_agent_env_file"; set +a
  eval "$_agent_env_keep"
fi
unset _agent_env_file _agent_env_keep
AGENT_OVERLAY_DIR="${AGENT_OVERLAY_DIR:+${AGENT_OVERLAY_DIR/#\~/$HOME}}"
need_host() { [ -n "${AGENT_RULES_HOST:-}" ] || { echo "${0##*/}: set AGENT_RULES_HOST (ssh host) in ${AGENT_SETUP_ENV:-~/.config/claude-agent-setup/env}" >&2; exit 64; }; }
