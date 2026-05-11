# NOTICE

fanout was inspired by two prior works:

- **Multi-agent dispatch pattern** — [LeuAlmeida/workstation](https://github.com/LeuAlmeida/workstation) (MIT). An earlier version of fanout forked workstation's tool inventory and built a Terraform-for-laptop layer on top of it. That version is preserved in git history (commits `550140c` through `1b2ab73`). The current fanout is a separate idea: parallel Claude agents in tmux panes, prompted by you or by an orchestrator-Claude.

- **HTML-as-output** — [Thariq's html-effectiveness](https://thariqs.github.io/html-effectiveness/). The aesthetic of `multi_agent_fanout.html` and earlier report-rendering experiments owe to that essay.

The current code (fanout.py, workers.py, prompts.py) is original to this repo, MIT-licensed.
