"""An `--agent` module with agents but no judge: --live needs one."""

from cli.eval_modules.eval_agents import support

agents = [support()]
