from typing import Any, Union

import torch
import torch.nn as nn
import torch.nn.functional as F

from torch.utils.data import DataLoader

from rl4co.envs.common.base import RL4COEnvBase
from rl4co.models.rl.common.base import RL4COLitModule
from rl4co.models.rl.common.critic import CriticNetwork, create_critic_from_actor
from rl4co.utils.pylogger import get_pylogger

log = get_pylogger(__name__)


class PPO(RL4COLitModule):
    """
    An implementation of the Proximal Policy Optimization (PPO) algorithm (https://arxiv.org/abs/1707.06347)
    is presented with modifications for autoregressive decoding schemes.

    In contrast to the original PPO algorithm, this implementation does not consider autoregressive decoding steps
    as part of the MDP transition. While many Neural Combinatorial Optimization (NCO) studies model decoding steps
    as transitions in a solution-construction MDP, we treat autoregressive solution construction as an algorithmic
    choice for tractable CO solution generation. This choice aligns with the Attention Model (AM)
    (https://openreview.net/forum?id=ByxBFsRqYm), which treats decoding steps as a single-step MDP in Equation 9.

    Modeling autoregressive decoding steps as a single-step MDP introduces significant changes to the PPO implementation,
    including:
    - Generalized Advantage Estimation (GAE) (https://arxiv.org/abs/1506.02438) is not applicable since we are dealing with a single-step MDP.
    - The definition of policy entropy can differ from the commonly implemented manner.

    The commonly implemented definition of policy entropy is the entropy of the policy distribution, given by:

    .. math:: H(\\pi(x_t)) = - \\sum_{a_t \\in A_t} \\pi(a_t|x_t) \\log \\pi(a_t|x_t)

    where :math:`x_t` represents the given state at step :math:`t`, :math:`A_t` is the set of all (admisible) actions
    at step :math:`t`, and :math:`a_t` is the action taken at step :math:`t`.

    If we interpret autoregressive decoding steps as transition steps of an MDP, the entropy for the entire decoding
    process can be defined as the sum of entropies for each decoding step:

    .. math:: H(\\pi) = \\sum_t H(\\pi(x_t))

    However, if we consider autoregressive decoding steps as an algorithmic choice, the entropy for the entire decoding
    process is defined as:

    .. math:: H(\\pi) = - \\sum_{a \\in A} \\pi(a|x) \\log \\pi(a|x)

    where :math:`x` represents the given CO problem instance, and :math:`A` is the set of all feasible solutions.

    Due to the intractability of computing the entropy of the policy distribution over all feasible solutions,
    we approximate it by computing the entropy over solutions generated by the policy itself. This approximation serves
    as a proxy for the second definition of entropy, utilizing Monte Carlo sampling.

    It is worth noting that our modeling of decoding steps and the implementation of the PPO algorithm align with recent
    work in the Natural Language Processing (NLP) community, specifically RL with Human Feedback (RLHF)
    (e.g., https://github.com/lucidrains/PaLM-rlhf-pytorch).
    """

    def __init__(
        self,
        env: RL4COEnvBase,
        policy: nn.Module,
        critic: CriticNetwork = None,
        critic_kwargs: dict = {},
        clip_range: float = 0.2,  # epsilon of PPO
        ppo_epochs: int = 2,  # inner epoch, K
        mini_batch_size: Union[int, float] = 0.25,  # 0.25,
        vf_lambda: float = 0.5,  # lambda of Value function fitting
        entropy_lambda: float = 0.0,  # lambda of entropy bonus
        normalize_adv: bool = False,  # whether to normalize advantage
        max_grad_norm: float = 0.5,  # max gradient norm
        metrics: dict = {
            "train": ["reward", "loss", "surrogate_loss", "value_loss", "entropy"],
        },
        **kwargs,
    ):
        super().__init__(env, policy, metrics=metrics, **kwargs)
        self.automatic_optimization = False  # PPO uses custom optimization routine

        if critic is None:
            log.info("Creating critic network for {}".format(env.name))
            critic = create_critic_from_actor(policy, **critic_kwargs)
        self.critic = critic

        if isinstance(mini_batch_size, float) and (
            mini_batch_size <= 0 or mini_batch_size > 1
        ):
            default_mini_batch_fraction = 0.25
            log.warning(
                f"mini_batch_size must be an integer or a float in the range (0, 1], got {mini_batch_size}. Setting mini_batch_size to {default_mini_batch_fraction}."
            )
            mini_batch_size = default_mini_batch_fraction

        if isinstance(mini_batch_size, int) and (mini_batch_size <= 0):
            default_mini_batch_size = 128
            log.warning(
                f"mini_batch_size must be an integer or a float in the range (0, 1], got {mini_batch_size}. Setting mini_batch_size to {default_mini_batch_size}."
            )
            mini_batch_size = default_mini_batch_size

        self.ppo_cfg = {
            "clip_range": clip_range,
            "ppo_epochs": ppo_epochs,
            "mini_batch_size": mini_batch_size,
            "vf_lambda": vf_lambda,
            "entropy_lambda": entropy_lambda,
            "normalize_adv": normalize_adv,
            "max_grad_norm": max_grad_norm,
        }

    def configure_optimizers(self):
        parameters = list(self.policy.parameters()) + list(self.critic.parameters())
        return super().configure_optimizers(parameters)

    def on_train_epoch_end(self):
        """
        ToDo: Add support for other schedulers.
        """

        sch = self.lr_schedulers()

        # If the selected scheduler is a MultiStepLR scheduler.
        if isinstance(sch, torch.optim.lr_scheduler.MultiStepLR):
            sch.step()

    def shared_step(
        self, batch: Any, batch_idx: int, phase: str, dataloader_idx: int = None
    ):
        # Evaluate old actions, log probabilities, and rewards
        with torch.no_grad():
            td = self.env.reset(batch)  # note: clone needed for dataloader
            out = self.policy(td.clone(), self.env, phase=phase, return_actions=True)

        if phase == "train":
            batch_size = out["actions"].shape[0]

            # infer batch size
            if isinstance(self.ppo_cfg["mini_batch_size"], float):
                mini_batch_size = int(batch_size * self.ppo_cfg["mini_batch_size"])
            elif isinstance(self.ppo_cfg["mini_batch_size"], int):
                mini_batch_size = self.ppo_cfg["mini_batch_size"]
            else:
                raise ValueError("mini_batch_size must be an integer or a float.")

            if mini_batch_size > batch_size:
                mini_batch_size = batch_size

            # Todo: Add support for multi dimensional batches
            td.set("logprobs", out["log_likelihood"])
            td.set("reward", out["reward"])
            td.set("action", out["actions"])

            # Inherit the dataset class from the environment for efficiency
            dataset = self.env.dataset_cls(td)
            dataloader = DataLoader(
                dataset,
                batch_size=mini_batch_size,
                shuffle=True,
                collate_fn=dataset.collate_fn,
            )

            for _ in range(self.ppo_cfg["ppo_epochs"]):  # PPO inner epoch, K
                for sub_td in dataloader:
                    sub_td = sub_td.to(td.device)
                    previous_reward = sub_td["reward"].view(-1, 1)
                    out = self.policy(  # note: remember to clone to avoid in-place replacements!
                        sub_td.clone(),
                        actions=sub_td["action"],
                        env=self.env,
                        return_entropy=True,
                        return_sum_log_likelihood=False,
                    )
                    ll, entropy = out["log_likelihood"], out["entropy"]

                    # Compute the ratio of probabilities of new and old actions
                    ratio = torch.exp(ll.sum(dim=-1) - sub_td["logprobs"]).view(
                        -1, 1
                    )  # [batch, 1]

                    # Compute the advantage
                    value_pred = self.critic(sub_td)  # [batch, 1]
                    adv = previous_reward - value_pred.detach()

                    # Normalize advantage
                    if self.ppo_cfg["normalize_adv"]:
                        adv = (adv - adv.mean()) / (adv.std() + 1e-8)

                    # Compute the surrogate loss
                    surrogate_loss = -torch.min(
                        ratio * adv,
                        torch.clamp(
                            ratio,
                            1 - self.ppo_cfg["clip_range"],
                            1 + self.ppo_cfg["clip_range"],
                        )
                        * adv,
                    ).mean()

                    # compute value function loss
                    value_loss = F.huber_loss(value_pred, previous_reward)

                    # compute total loss
                    loss = (
                        surrogate_loss
                        + self.ppo_cfg["vf_lambda"] * value_loss
                        - self.ppo_cfg["entropy_lambda"] * entropy.mean()
                    )

                    # perform manual optimization following the Lightning routine
                    # https://lightning.ai/docs/pytorch/stable/common/optimization.html

                    opt = self.optimizers()
                    opt.zero_grad()
                    self.manual_backward(loss)
                    if self.ppo_cfg["max_grad_norm"] is not None:
                        self.clip_gradients(
                            opt,
                            gradient_clip_val=self.ppo_cfg["max_grad_norm"],
                            gradient_clip_algorithm="norm",
                        )
                    opt.step()

            out.update(
                {
                    "loss": loss,
                    "surrogate_loss": surrogate_loss,
                    "value_loss": value_loss,
                    "entropy": entropy.mean(),
                }
            )

        metrics = self.log_metrics(out, phase, dataloader_idx=dataloader_idx)
        return {"loss": out.get("loss", None), **metrics}
