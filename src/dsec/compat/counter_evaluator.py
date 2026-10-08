"""Legacy counter demo's grading rules, outside generic worker lifecycle code."""
from dsec.contracts.evaluation import EvaluationOutcome


class CounterEvaluator:
    id = "counter-exact-value-v1"
    command = "cat /rl-counter"

    @staticmethod
    def expected(parameters):
        value = parameters.get("expected_counter")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("expected_counter must be a nonnegative integer")
        return value

    def validate(self, context, parameters):
        self.expected(parameters)

    def accepts_reward(self, reward, parameters):
        return bool(reward and reward.get("expected_counter") == self.expected(parameters))

    @staticmethod
    def reward_from_result(result, expected):
        observed = result["output"].strip()
        success = result["exit_code"] == 0 and observed == str(expected)
        return {"value": 1.0 if success else 0.0, "expected_counter": expected,
                "observed": observed, "verifier_exit_code": result["exit_code"]}

    async def evaluate(self, context, sandbox, parameters):
        expected = self.expected(parameters)
        result = await sandbox.run_shell(self.command)
        return EvaluationOutcome(self.reward_from_result(result, expected))
