import os
from typing import List, Union

import numpy as np
import torch
from rich.console import Console
from rich.table import Table

import trlx.utils.logging as logging
from trlx.orchestrator import Orchestrator, register_orchestrator
from trlx.pipeline.offline_pipeline import ILQLRolloutStorage

logger = logging.get_logger(__name__)


def tokenize_dialogue(  # noqa: C901
    dialogue: Union[str, List[str]], tokenizer, max_length=2048, truncation_side="left"
) -> List[int]:
    """
    Tokenize sample with the interleaved form of (prompt_1, output_1, prompt_2, output_2...)
    """
    if isinstance(dialogue, str):
        dialogue = [tokenizer.bos_token, dialogue]
    elif isinstance(dialogue, tuple):
        dialogue = list(dialogue)
    dialogue[-1] += tokenizer.eos_token

    out = []
    ctx_length = max_length
    if tokenizer.truncation_side == "left":
        for phrase in reversed(dialogue):
            tokens = tokenizer(phrase).input_ids[-ctx_length:]
            ctx_length -= len(tokens)
            out.insert(0, tokens)
            if ctx_length == 0:
                break

        # in case of odd number of phrases (possibly due to truncation)
        # since the first phrase always has to be a prompt, force it to be <bos>
        if len(out) % 2 == 1:
            if sum(map(len, out)) == max_length:
                out[0].pop(0)
            out.insert(0, [tokenizer.bos_token_id])

    elif tokenizer.truncation_side == "right":
        for phrase in dialogue:
            tokens = tokenizer(phrase).input_ids[:ctx_length]
            ctx_length -= len(tokens)
            out.append(tokens)
            if ctx_length == 0:
                break
    return out


@register_orchestrator
class OfflineOrchestrator(Orchestrator):
    """
    Orchestrator that creates a static dataset for offline training
    """

    def __init__(self, trainer):
        self.trainer = trainer

    def make_experience(self, samples, rewards, max_length=2048):
        """
        Tokenizes samples and shapes rewards into proper tensors and then inserts the resulting dataset into the trainer
        """
        logger.info("Collecting rollouts")

        if self.trainer.tokenizer:
            samples = [tokenize_dialogue(s, self.trainer.tokenizer, max_length) for s in samples]

        all_input_ids = []
        all_actions_ixs = []
        all_states_ixs = []
        all_dones = []
        for sample in samples:
            length = 0
            all_input_ids.append(torch.tensor(sum(sample, [])))
            isoutput = False
            actions_ixs = []
            for phrase in sample:
                if isoutput:
                    actions_ixs.append(torch.arange(length - 1, length + len(phrase) - 1))

                length += len(phrase)
                isoutput = not isoutput

            states_ixs = torch.hstack((*actions_ixs, torch.tensor(length - 1)))
            all_dones.append(torch.tensor([1] * (len(states_ixs) - 1) + [0], dtype=int))
            all_actions_ixs.append(torch.hstack(actions_ixs))
            all_states_ixs.append(states_ixs)

        if self.trainer.tokenizer and os.environ.get("RANK", "0") == "0":
            logger.info("Logging sample example")
            prompt = self.trainer.tokenizer.decode(all_input_ids[0][: all_states_ixs[0][1]])
            response = self.trainer.tokenizer.decode(all_input_ids[0][all_states_ixs[0][1] :])
            columns = ["Prompt", "Response", "Reward"]
            table = Table(*columns, title="Sample Example", show_lines=True)
            table.add_row(prompt, response, str(rewards[0]))
            Console().print(table)

        sample_lengths = np.array(list(map(len, all_input_ids)))
        output_lengths = np.array(list(map(len, all_actions_ixs)))
        prompt_lengths = sample_lengths - output_lengths
        returns = torch.tensor(rewards, dtype=float)

        if os.environ.get("RANK", "0") == "0":
            logger.info("Logging experience string statistics")
            columns = ["Prompt Length", "Output Length", "Sample Length"]
            table = Table(*columns, title="Experience String Stats (mean ∈ \[min, max])", show_lines=True)
            row = []
            for lengths in [prompt_lengths, output_lengths, sample_lengths]:
                row.append(f"{lengths.mean():.2f} ∈ [{min(lengths)}, {max(lengths)}]")
            table.add_row(*row)
            Console().print(table)

        returns = (returns - returns.mean()) / (returns.std() + 1e-30)
        rewards = [torch.zeros(len(x)) for x in all_actions_ixs]
        for rs, ret in zip(rewards, returns):
            rs[-1] = ret

        attention_mask = [torch.ones(len(x), dtype=int) for x in all_input_ids]

        self.trainer.store = ILQLRolloutStorage(
            all_input_ids,
            attention_mask,
            rewards,
            all_states_ixs,
            all_actions_ixs,
            all_dones,
        )
