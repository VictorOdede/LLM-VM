import abc
from abc import ABC,abstractmethod
import openai
import math
from transformers import (
    AutoModelForCausalLM,
    AutoModelForMaskedLM,
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    BertTokenizer,
    OPTForCausalLM,
    BloomForCausalLM,
    LlamaTokenizer,
    LlamaForCausalLM,
    GPTNeoForCausalLM,
    GPTNeoXForCausalLM,
    GPT2Tokenizer,
    DataCollatorForLanguageModeling,
    TrainingArguments,
    Trainer,
    LogitsProcessorList,
    LogitsProcessor
    )
import time
from datetime import datetime
import tempfile
import json
import os
import torch
import re
from itertools import chain, combinations

__private_key_value_models_map =  {}
# []   {
#         "opt": Small_Local_OPT,
#         "bloom": Small_Local_Bloom,
#         "neo": Small_Local_Neo,
#         "llama": Small_Local_LLama,
#         "pythia": Small_Local_Pythia,
#         "gpt": GPT3,
#         "chat_gpt": Chat_GPT,
#         "flan" : Small_Local_Flan_T5,
#         "pythia" : Small_Local_Pythia,
#         }

def RegisterModelClass(name):
    def regClass(cls):
        __private_key_value_models_map[name]=cls 
    return regClass

model_keys_registered = __private_key_value_models_map.keys()        
# Dictionary of models to be loaded in ModelConfig
def load_model_closure(model_name):
    models = __private_key_value_models_map
    return models[model_name]

# this is a hack till we add dynaconf or something?
if os.name == "nt":
    homepath = os.path.join('C:\\','Users',os.getlogin())
else:
    homepath = os.environ.get("HOME")

model_path_default = os.path.join( homepath , ".llm_vm", "models")
os.makedirs(model_path_default, exist_ok = True)

def create_jsonl_file(data_list):
    out = tempfile.TemporaryFile('w+')
    for a,b in data_list:
        out.write(json.dumps({'prompt': a, 'completion': b}) + "\n")
    out.seek(0)
    return out


class FinetuningDataset(torch.utils.data.Dataset):
    def __init__(self,iterable_dataset,length):
        self.dataset = list(iterable_dataset)
        self.length = length
    def __len__(self):
        return self.length
    def __getitem__(self, idx):
        return self.dataset[idx]

class Base_Onsite_LLM(ABC):
    def __init__(self,model_uri=None,tokenizer_kw_args={},model_kw_args={}):
        if model_uri != None :
            self.model_uri= model_uri
        if model_uri is None and self.model_uri is None:
            raise ValueError('A very specific bad thing happened.')
        self.model_name : str = self.model_uri.split('/')[-1] # our default for deriving model name
        self.model=self.model_loader(**model_kw_args)
        self.tokenizer=self.tokenizer_loader(**tokenizer_kw_args)

    @property
    @abstractmethod
    def model_uri(self):
        pass

    @model_uri.setter
    def model_uri(self,val):
        self.model_uri=val # check if this is correct

    # model_name : str = self.model_uri.split('/')[-1]

    @abstractmethod
    def model_loader(self):
        pass

    @abstractmethod
    def tokenizer_loader(self):
        pass

    def load_finetune(self, model_filename):
        self.model.load_state_dict(torch.load(os.path.join(model_path_default,"finetuned_models", self.model_name, model_filename)))


    def generate(self,prompt,max_length=100,**kwargs): # both tokenizer and model take kwargs :(
        """
        This function uses the class's llm and tokenizer to generate a response given a user's prompt

        Parameters:
            prompt (str): Prompt to send to LLM
            max_length (int): Optional parameter limiting response length


        Returns:
            str: LLM Generated Response

        Example:
           >>> Small_Local_OPT.generate("How long does it take for an apple to grow?)
           I think it takes about a week for the apple to grow.
        """
        inputs=self.tokenizer(prompt,return_tensors="pt")
        generate_ids=self.model.generate(inputs.input_ids,max_length=max_length)
        resp= self.tokenizer.batch_decode(generate_ids,skip_special_tokens=True,clean_up_tokenization_spaces=False)[0]
        # need to drop the len(prompt) prefix with these sequences generally
        # because they include the prompt.
        return resp[len(prompt):]

    def finetune(self,data, optimizer, c_id):
        def asynctune():
            old_model = optimizer.storage.get_model(c_id)
            if old_model is not None:
                self.model.load_state_dict(torch.load(old_model))
            untokenized_final_dataset = []
            for prompt,response in data:
                untokenized_final_dataset.append(prompt + response)
            tokenized_final_dataset = map(self.tokenizer,untokenized_final_dataset)
            self.tokenizer.pad_token = self.tokenizer.eos_token
            data_collator = DataCollatorForLanguageModeling(tokenizer=self.tokenizer, mlm=False)
            optimizer.storage.set_training_in_progress(c_id, True)
            training_args = TrainingArguments(
                output_dir=os.path.join(model_path_default,"finetuned_models",),
                evaluation_strategy="epoch",
                learning_rate=2e-5,
                per_device_train_batch_size = 1,
                per_device_eval_batch_size = 1,
                num_train_epochs=1,
                weight_decay=0.01,
                report_to= "none",
            )
            test_set = FinetuningDataset(tokenized_final_dataset,len(untokenized_final_dataset))

            trainer = Trainer(
                model=self.model,
                args=training_args,
                train_dataset=test_set,
                eval_dataset=test_set,
                data_collator=data_collator,
            )
            os.makedirs(os.path.join(model_path_default,"finetuned_models", self.model_name), exist_ok=True)
            if tokenized_final_dataset:
                trainer.train()
                eval_results = trainer.evaluate()
            optimizer.storage.set_training_in_progress(c_id, False)

            if os.name == "nt":
                timestamp = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
            else:
                timestamp = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            new_model = os.path.join(model_path_default,"finetuned_models",self.model_name, timestamp + '_' + self.model_name + ".pt" )
            open(new_model,"a")
            torch.save(self.model.state_dict(), new_model) # the model in memory is different now
            self.model_name = self.model_name + "_ft_"+  timestamp
            optimizer.storage.set_model(c_id, new_model)
            return math.exp(eval_results['eval_loss']) #perplexity is the metric we use for finetuning measurement
        return asynctune


    def finetune_immediately(self):
        finetune()()

class TokenConstraint(ABC):
    def __init__(self, constraint_type, state_type, model_uri):
        self.constraint_type = constraint_type
        self.state_type = state_type
        self.model_uri = model_uri
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_uri, add_prefix_space=True)

    def construct_crude_filter_set(self, expression):
        vocab = self.tokenizer.vocab
        if self.constraint_type == 'regex':
            vocab_map = {v: k for k, v in vocab.items()}
            space_repr = self.tokenizer.tokenize(" ")[0]
            print(space_repr)
            nl_repr = self.tokenizer.tokenize("\n")[0]
            print(nl_repr)
            expression = expression.replace(" ", space_repr)
            expression = expression.replace("\n", nl_repr)
            expression = expression.replace(" ", self.tokenizer.tokenize(" ")[0])

        valid_ids = []
        pattern = re.compile(expression, re.UNICODE)
        for id, subtoken in vocab_map.items():
            if pattern.match(subtoken) is not None:
                valid_ids.append(id)

        return valid_ids

    def _regex_to_nfa(self, infix):
        def infix_to_postfix(infix):
            # Dictionary for special characters gives them an order of precedence
            specials = {'*': 60, '+': 55, '?': 50, '.': 40, '|': 20}
            postfix, stack = "", ""

            for c in infix:
                if c == '(':
                    stack = stack + c
                elif c == ')':
                    while stack[-1] != '(':  
                        postfix = postfix + stack[-1]  
                        stack = stack[:-1] 
                    stack = stack[:-1]  
                elif c in specials:
                    while stack and specials.get(c, 0) <= specials.get(stack[-1], 0):
                        postfix, stack = postfix + stack[-1], stack[:-1]
                    stack = stack + c
                else:
                    postfix = postfix + c
            while stack:
                postfix, stack = postfix + stack[-1], stack[:-1]

            return postfix

        # Thompsons construction Algorithm
        class State:
            label, edge1, edge2 = None, None, None
        state = {"label": None, "edge1": None, "edge2": None}
        
        class NFA:
            initial, accept = None, None
            
            def __init__(self, initial, accept):
                self.initial, self.accept = initial, accept
        
        nfa = {"initial": None, "accept": None}

        def compile(postfix):
            nfaStack = []
            for c in postfix:
                if c == '*':
                    pass
                #     nfa1 = nfaStack.pop()
                #     initial, accept = State(), State()
                #     initial.edge1, initial.edge2 = nfa1.initial, accept
                #     nfa1.accept.edge1, nfa1.accept.edge2 = nfa1.initial, accept
                #     nfaStack.append(NFA(initial, accept))
                # elif c == '.':
                #     nfa2, nfa1 = nfaStack.pop(), nfaStack.pop()
                #     nfa1.accept.edge1 = nfa2.initial
                #     nfaStack.append(NFA(nfa1.initial, nfa2.accept))
                # elif c == '|':
                #     nfa2, nfa1 = nfaStack.pop(), nfaStack.pop()
                #     initial = State()
                #     initial.edge1, initial.edge2 = nfa1.initial, nfa2.initial
                #     accept = State()
                #     nfa1.accept.edge1, nfa2.accept.edge1 = accept, accept
                #     nfaStack.append(NFA(initial, accept))
                # elif c == '+':
                #     nfa1 = nfaStack.pop()
                #     accept, initial = State(), State()
                #     initial.edge1 = nfa1.initial
                #     nfa1.accept.edge1, nfa1.accept.edge2 = nfa1.initial, accept
                #     nfaStack.append(NFA(initial, accept))
                # elif c == '?':
                #     nfa1 = nfaStack.pop()
                #     accept, initial = State(), State()
                #     initial.edge1, initial.edge2 = nfa1.initial, accept
                #     nfa1.accept.edge1 = accept
                #     nfaStack.append(NFA(initial, accept))
                else:
                    print(nfaStack)
                    # accept, initial = State(), State()
                    # initial.label, initial.edge1 = c, accept
                    # nfaStack.append(NFA(initial, accept))
                    accept, initial = state, state
                    initial["label"] = c
                    initial["edge1"] = accept
                    curr_nfa = nfa
                    curr_nfa["initial"] = initial
                    curr_nfa["accept"] = accept
                    nfaStack.append(curr_nfa)
            return nfaStack.pop()
        
        # class DFA:
        #     STATUS_NUM = 0

        #     def __init__(self):
        #         self.nfa_sets = []
        #         self.accepted = False
        #         self.status_num = -1

        #     @classmethod
        #     def nfa_to_dfa(cls, nfa):
        #         dfa = cls()
        #         for n in nfa:
        #             dfa.nfa_sets.append(n)
        #             if n.next_1 is None and n.next_2 is None:
        #                 dfa.accepted = True

        #         dfa.status_num = DFA.STATUS_NUM
        #         DFA.STATUS_NUM = DFA.STATUS_NUM + 1
        #         return dfa
        
        postf = infix_to_postfix(infix)
        res = compile(postf)
        print(res)

        def follow_edges(state):
            states = set()
            states.add(state)

            if state.label is None:
                if state.edge1 is not None:
                    states |= follow_edges(state.edge1)
                if state.edge2 is not None:
                    states |= follow_edges(state.edge2)

            return states

        def match(infix, string):
            postfix = infix_to_postfix(infix)
            nfa = compile(postfix)
            current = set()
            nexts = set()

            current |= follow_edges(nfa.initial)

            # loop through each character in the string
            for s in string:
                # loop through the current set of states
                for c in current:
                # Check to see if state is labelled 's'
                    if c.label == s:
                        nexts |= follow_edges(c.edge1)
                # set current to next and clears out next
                current = nexts
                # next is back to an empty set
                nexts = set()
            # Checks if the accept state is in the set for current state
            return (nfa.accept in current)

class TokenStreamerAsStoppingCriterion:
    def __init__(self, token_streamer):
        self.token_streamer = token_streamer

    def __call__(self, input_ids, scores, **kwargs):
        if self.token_streamer is None:
            return None
        else:
            self.token_streamer(input_ids, scores, **kwargs)
            return False


class RegexBiasLogitsProcessor(LogitsProcessor):
        def __init__(self, sequence_bias):
            self.sequence_bias = sequence_bias
            self._validate_arguments()

            # Bias variables that will be populated on the first call (for retrocompatibility purposes, the vocabulary size
            # is infered in the first usage, which inhibits initializing here)
            self.sequences_length_greater_than_1 = []
            self.length_1_bias = None
            self.length_greather_than_1_bias = None
            self.prepared_bias_variables = False

        def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> torch.FloatTensor:
            # 1 - Prepares the bias tensors. This is only needed the first time the logit processor is called.
            if not self.prepared_bias_variables:
                self._prepare_bias_variables(scores)

            # 2 - prepares an empty bias to add
            bias = torch.zeros_like(scores)

            # 3 - include the bias from length = 1
            bias += self.length_1_bias

            # 4 - include the bias from length > 1, after determining which biased sequences may be completed.
            # `matching_mask` is a (batch_size, vocab_size) boolean mask that is True for all tokens whose corresponding
            # bias should be applied. The bias is applied on the last token of the sequence, if (and only if) the sequence
            # may become complete this iteration.
            matching_mask = torch.zeros_like(scores, dtype=torch.bool)
            for sequence_ids in self.sequences_length_greater_than_1:
                if len(sequence_ids) > input_ids.shape[1]:  # the sequence is longer than the context, ignore
                    continue
                prefix_length = len(sequence_ids) - 1
                last_token = sequence_ids[-1]
                matching_rows = torch.eq(
                    input_ids[:, -prefix_length:],
                    torch.tensor(sequence_ids[:-1], dtype=input_ids.dtype, device=input_ids.device),
                ).prod(dim=1)
                matching_mask[:, last_token] |= matching_rows.bool()
            bias += torch.where(
                matching_mask,
                self.length_greather_than_1_bias,
                torch.tensor(0.0, device=self.length_greather_than_1_bias.device),
            )

            # 5 - apply the bias to the scores
            scores = scores + bias
            return scores

        def _prepare_bias_variables(self, scores: torch.FloatTensor):
            vocabulary_size = scores.shape[-1]
            sequence_bias = self.sequence_bias
            tokens_with_bias = []

            # Check biased tokens out of bounds
            invalid_biases = []
            for sequence_ids in sequence_bias:
                for token_id in sequence_ids:
                    if token_id >= vocabulary_size:
                        invalid_biases.append(token_id)
            if len(invalid_biases) > 0:
                raise ValueError(
                    f"The model vocabulary size is {vocabulary_size}, but the following tokens were being biased: "
                    f"{invalid_biases}"
                )

            # Precompute the bias tensors to be applied. Sequences of length 1 are kept separately, as they can be applied
            # with simpler logic.
            self.length_1_bias = torch.zeros((vocabulary_size,), dtype=torch.float).to(scores.device)
            self.length_greather_than_1_bias = torch.zeros((vocabulary_size,), dtype=torch.float).to(scores.device)
            for sequence_ids, bias in sequence_bias.items():
                if len(sequence_ids) == 1:
                    self.length_1_bias[sequence_ids[-1]] = bias
                else:
                    self.sequences_length_greater_than_1.append(sequence_ids)
                    if self.length_greather_than_1_bias[sequence_ids[-1]] != 0.0:
                        raise ValueError(
                            "Setting a bias on sequences that share a common token termination is not yet supported. "
                            "Please open an issue if you see this error message (after checking that it doesn't already "
                            "exist)."
                        )
                    self.length_greather_than_1_bias[sequence_ids[-1]] = bias
                tokens_with_bias.append(sequence_ids[-1])

            self.prepared_bias_variables = True

        def _validate_arguments(self):
            sequence_bias = self.sequence_bias
            if not isinstance(sequence_bias, dict) or len(sequence_bias) == 0:
                raise ValueError(f"`sequence_bias` has to be a non-empty dictionary, but is {sequence_bias}.")
            if any(not isinstance(sequence_ids, tuple) for sequence_ids in sequence_bias.keys()):
                raise ValueError(f"`sequence_bias` has to be a dict with tuples as keys, but is {sequence_bias}.")
            if any(
                any((not isinstance(token_id, (int, torch.int)) or token_id < 0) for token_id in sequence_ids)
                or len(sequence_ids) == 0
                for sequence_ids in sequence_bias.keys()
            ):
                raise ValueError(
                    f"Each key in `sequence_bias` has to be a non-empty tuple of positive integers, but is "
                    f"{sequence_bias}."
                )
            if any(not isinstance(bias, float) for bias in sequence_bias.values()):
                raise ValueError(f"`sequence_bias` has to be a dict with floats as values, but is {sequence_bias}.")



class HFTransformersWithConstraints:
    def __init__(self, model_uri, **kwargs):
        self.model_identifier = model_uri
        self.model_args = kwargs

        print(f"Creating {self.model_identifier} instance using AutoModelForCausalLM transformers module", flush=True)
        self.model = AutoModelForCausalLM.from_pretrained(self.model_identifier, **self.model_args)
        print(f"{self.model_identifier} model is ready for use on {self.model.device}", flush=True)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_identifier, add_prefix_space=True)
        self.vocab_size = self.tokenizer.vocab_size
        self.vocab = self.tokenizer.vocab

    @property
    def eos_token_id(self):
        return self.model.config.eos_token_id

    
    def generate(self, input_ids, attention_mask, temperature, max_new_tokens, streamer, regex, r_bias):
        assert torch.is_tensor(input_ids), "Input ids must be a torch tensor"
        assert torch.is_tensor(attention_mask), "Attention mask must be a torch tensor"    

        legal_tokens = self._make_mask_from_regex(regex)

        seq_bias = {}
        for t in legal_tokens:
            t_tuple = tuple(self.tokenizer.encode(t, add_special_tokens=False))
            seq_bias[t_tuple] = r_bias

        logits_processor = LogitsProcessorList()
        logits_processor.append(RegexBiasLogitsProcessor(seq_bias))
        
        kwargs = {
            "input_ids": input_ids.to(self.model.device),
            "do_sample": temperature > 0.0,
            "attention_mask": attention_mask.to(self.model.device),
            "temperature": temperature,
            "max_new_tokens": max_new_tokens,
            "logits_processor": logits_processor,
            "output_scores": True,
            "return_dict_in_generate": True
        }

        result = self.model.generate(**kwargs, stopping_criteria=[TokenStreamerAsStoppingCriterion(streamer)], 
                                     eos_token_id=self.eos_token_id, pad_token_id=self.eos_token_id)

        return (result.sequences, result.scores)


"""
this factorization isn't necessarily the greatest, nor should it be viewed
as likely being more general, aside from covering hugging face transformers
"""
@RegisterModelClass("pythia")
class Small_Local_Pythia(Base_Onsite_LLM):
    """
    This is a class for ElutherAI's Pythia-70m LLM

    Attributes:
        model_uri (str): Hugging Face Endpoint for LLM
        tokenizer (AutoTokenizer): Tokenizer from Transformer's library
        model (LLM): The large language model

    Methods:
        model_loader: Loads the LLM into memory
        tokenizer_loader: Loads the tokenizer into memory
        generate: Generates a response from a given prompt with the loaded LLM and tokenizer
    """
    # def __init__(self,**kwargs):
    #     # self.model_uri =
    #     super().__init__(kwargs) ## this line is required
    model_uri = "EleutherAI/pythia-70m-deduped"
    def model_loader(self):
        return GPTNeoXForCausalLM.from_pretrained(self.model_uri)
    def tokenizer_loader(self):
        return AutoTokenizer.from_pretrained(self.model_uri)


@RegisterModelClass("opt")
class Small_Local_OPT(Base_Onsite_LLM):

    """
    This is a class for Facebook's OPT-350m LLM

    Attributes:
        model_uri (str): Hugging Face Endpoint for LLM
        tokenizer (AutoTokenizer): Tokenizer from Transformer's library
        model (LLM): The large language model

    Methods:
        model_loader: Loads the LLM into memory
        tokenizer_loader: Loads the tokenizer into memory
        generate: Generates a response from a given prompt with the loaded LLM and tokenizer
    """
    model_uri="facebook/opt-350m"
    def model_loader(self):
        return OPTForCausalLM.from_pretrained(self.model_uri)
    def tokenizer_loader(self):
        return AutoTokenizer.from_pretrained(self.model_uri)

@RegisterModelClass("bloom")
class Small_Local_Bloom(Base_Onsite_LLM):

    """
    This is a class for BigScience's bloom-560 LLM

    Attributes:
        model_uri (str): Hugging Face Endpoint for LLM
        tokenizer (AutoTokenizer): Tokenizer from Transformer's library
        model (LLM): The large language model

    Methods:
        model_loader: Loads the LLM into memory
        tokenizer_loader: Loads the tokenizer into memory
        generate: Generates a response from a given prompt with the loaded LLM and tokenizer
    """
    model_uri="bigscience/bloom-560m"

    def model_loader(self):
        return BloomForCausalLM.from_pretrained(self.model_uri)
    def tokenizer_loader(self):
        return AutoTokenizer.from_pretrained(self.model_uri)

@RegisterModelClass("neo")
class Small_Local_Neo(Base_Onsite_LLM):

    """

    Attributes:
        model_uri (str): Hugging Face Endpoint for LLM
        tokenizer (AutoTokenizer): Tokenizer from Transformer's library
        model (LLM): The large language model

    Methods:
        model_loader: Loads the LLM into memory
        tokenizer_loader: Loads the tokenizer into memory
        generate: Generates a response from a given prompt with the loaded LLM and tokenizer
    """
    model_uri="EleutherAI/gpt-neo-1.3B"

    def model_loader(self):
        return GPTNeoForCausalLM.from_pretrained(self.model_uri)
    def tokenizer_loader(self):
        return GPT2Tokenizer.from_pretrained(self.model_uri)

@RegisterModelClass("llama")
class Small_Local_LLama(Base_Onsite_LLM):

    """
    This is a class for Openlm-Research's open_llama-3b LLM

    Attributes:
        model_uri (str): Hugging Face Endpoint for LLM
        tokenizer (AutoTokenizer): Tokenizer from Transformer's library
        model (LLM): The large language model

    Methods:
        model_loader: Loads the LLM into memory
        tokenizer_loader: Loads the tokenizer into memory
        generate: Generates a response from a given prompt with the loaded LLM and tokenizer
    """
    model_uri="openlm-research/open_llama_3b_v2"

    def model_loader(self):
        return LlamaForCausalLM.from_pretrained(self.model_uri)
    def tokenizer_loader(self):
        return LlamaTokenizer.from_pretrained(self.model_uri)

@RegisterModelClass("flan")# our yummiest model based on similarity to food
class Small_Local_Flan_T5(Base_Onsite_LLM):

    """
    This is a class for Google's flan-t5 LLM

    Attributes:
        model_uri (str): Hugging Face Endpoint for LLM
        tokenizer (AutoTokenizer): Tokenizer from Transformer's library
        model (LLM): The large language model

    Methods:
        model_loader: Loads the LLM into memory
        tokenizer_loader: Loads the tokenizer into memory
        generate: Generates a response from a given prompt with the loaded LLM and tokenizer
    """

    model_uri="google/flan-t5-small"
    def model_loader(self):
        return AutoModelForSeq2SeqLM.from_pretrained(self.model_uri)
    def tokenizer_loader(self):
        return AutoTokenizer.from_pretrained(self.model_uri)

@RegisterModelClass("bert")
class Small_Local_BERT(Base_Onsite_LLM):

    """
    This is a class for BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding
    The base model needs finetuning in almost all cases.

    Attributes:
        model_uri (str): Hugging Face Endpoint for LLM
        tokenizer (AutoTokenizer): Tokenizer from Transformer's library
        model (LLM): The large language model

    Methods:
        model_loader: Loads the LLM into memory
        tokenizer_loader: Loads the tokenizer into memory
        generate: Generates a response from a given prompt with the loaded LLM and tokenizer
    """

    model_uri = "bert-base-cased"
    def model_loader(self):
        return AutoModelForMaskedLM.from_pretrained(self.model_uri)
    def tokenizer_loader(self):
        return BertTokenizer.from_pretrained(self.model_uri)
@RegisterModelClass("gpt")
class GPT3:

    """
    This is a class for openAI's completion endpoint

    Methods:
        generate: Generates a response from a given prompt with OpenAI's completion endpoint
    """

    def generate(self,prompt, max_length=100,**kwargs): # both tokenizer and model take kwargs :(
        """
        This function uses openAI's API to generate a response from the prompt

        Parameters:
            prompt (str): Prompt to send to LLM
            max_length (int): Optional parameter limiting response length


        Returns:
            str: LLM Generated Response

        Example:
            >>> Small_Local_OPT.generate("How long does it take for an apple to grow?)
            It typically takes about 100-200 days...
        """

        ans = openai.Completion.create(prompt= prompt, model="text-davinci-003", **kwargs)
        return ans['choices'][0]['text']


    def finetune(self, dataset, optimizer, c_id):
        old_model = optimizer.storage.get_model(c_id)
        training_file = create_jsonl_file(dataset)
        upload_response = openai.File.create(file=training_file, purpose="fine-tune")
        training_file.close()
        fine_tuning_job = openai.FineTune.create(training_file= upload_response.id)

        print(f"Fine-tuning job created: {fine_tuning_job}", flush=True)
        global job_id # global state isn't great, but thats interrupt handlers
        job_id = fine_tuning_job["id"]
        while True:
            fine_tuning_status = openai.FineTune.retrieve(id=job_id)
            status = fine_tuning_status["status"]
            print(f"Fine-tuning job status: {status}")
            if status in ["succeeded", "completed", "failed"]:
                break
            time.sleep(30)
        job_id = None #
        new_model_id = fine_tuning_status.fine_tuned_model

        print("New_model_id: ", new_model_id, flush=True)

        optimizer.storage.set_model(c_id, new_model_id)
        optimizer.storage.set_training_in_progress(c_id, False)
        if old_model is not None:
            openai.Model.delete(old_model)

@RegisterModelClass("chat_gpt")
class Chat_GPT:
    """
    This is a class for openAI's gpt-3.5-turbo LLM

    Methods:
        generate: Generates a response from a given prompt through OpenAI's endpoint
    """

    def generate(self,prompt, max_length=100,**kwargs): # both tokenizer and model take kwargs :(
        """
        This function uses openAI's API to generate a response from the prompt

        Parameters:
            prompt (str): Prompt to send to LLM
            max_length (int): Optional parameter limiting response length


        Returns:
            str: LLM Generated Response

        Example:
            >>> Small_Local_OPT.generate("How long does it take for an apple to grow?)
            It typically takes about 100-200 days...
        """
        cur_prompt = [{'role': "system", 'content' : prompt}]
        ans = openai.ChatCompletion.create(
            messages=cur_prompt,
            model="gpt-3.5-turbo-0301",
            **kwargs)
        return ans['choices'][0]['message']['content']

    def finetune(self, dataset, optimizer, c_id):
        print("fine tuning isn't supported by OpenAI on this model")
        exit()
        # old_model = optimizer.storage.get_model(c_id)
        # training_file = create_jsonl_file(dataset)
        # upload_response = openai.File.create(file=training_file, purpose="fine-tune")
        # training_file.close()
        # fine_tuning_job = openai.FineTune.create(training_file= upload_response.id)

        # print(f"Fine-tuning job created: {fine_tuning_job}", flush=True)
        # global job_id # global state isn't great, but thats interrupt handlers
        # job_id = fine_tuning_job["id"]
        # while True:
        #     fine_tuning_status = openai.FineTune.retrieve(id=job_id)
        #     status = fine_tuning_status["status"]
        #     print(f"Fine-tuning job status: {status}")
        #     if status in ["succeeded", "completed", "failed"]:
        #         break
        #     time.sleep(30)
        # job_id = None #
        # new_model_id = fine_tuning_status.fine_tuned_model

        # print("New_model_id: ", new_model_id, flush=True)

        # optimizer.storage.set_model(c_id, new_model_id)
        # optimizer.storage.set_training_in_progress(c_id, False)
        # if old_model is not None:
        #     openai.Model.delete(old_model)


if __name__ == "__main__":
    # interface = TokenConstraint("regex","full-match","facebook/opt-350m")
    # tokenizer = AutoTokenizer.from_pretrained("facebook/opt-350m", add_prefix_space=True)

    # input_text = "The one performing the heart surgery is a"
    # input_ids = tokenizer(input_text, return_tensors="pt")
    # model_input = input_ids["input_ids"]
    # re_exp = r"doctor|person"
    # re_str = "doctor|person"
    # res = interface._regex_to_nfa(re_exp)
    # print(res)

    non_symbols = ['+', '*', '.', '(', ')']
    nfa = {}
    dfa = {}
    nfa_states = []
    dfa_states = []


    class charType:
        SYMBOL = 1
        CONCAT = 2
        UNION  = 3
        KLEENE = 4


    class NFAState:
        def __init__(self):
            self.next_state = {}


    class ExpressionTree:

        def __init__(self, charType, value=None):
            self.charType = charType
            self.value = value
            self.left = None
            self.right = None
        

    def make_exp_tree(regexp):
        stack = []
        for c in regexp:
            if c == "+":
                z = ExpressionTree(charType.UNION)
                z.right = stack.pop()
                z.left = stack.pop()
                stack.append(z)
            elif c == ".":
                z = ExpressionTree(charType.CONCAT)
                z.right = stack.pop()
                z.left = stack.pop()
                stack.append(z)
            elif c == "*":
                z = ExpressionTree(charType.KLEENE)
                z.left = stack.pop() 
                stack.append(z)
            elif c == "(" or c == ")":
                continue  
            else:
                stack.append(ExpressionTree(charType.SYMBOL, c))
        return stack[0]


    def compPrecedence(a, b):
        p = ["+", ".", "*"]
        return p.index(a) > p.index(b)


    def compute_regex(exp_t):
        # returns E-NFA
        if exp_t.charType == charType.CONCAT:
            return do_concat(exp_t)
        elif exp_t.charType == charType.UNION:
            return do_union(exp_t)
        elif exp_t.charType == charType.KLEENE:
            return do_kleene_star(exp_t)
        else:
            return eval_symbol(exp_t)


    def eval_symbol(exp_t):
        start = NFAState()
        end = NFAState()
        
        start.next_state[exp_t.value] = [end]
        return start, end


    def do_concat(exp_t):
        left_nfa  = compute_regex(exp_t.left)
        right_nfa = compute_regex(exp_t.right)

        left_nfa[1].next_state['$'] = [right_nfa[0]]
        return left_nfa[0], right_nfa[1]


    def do_union(exp_t):
        start = NFAState()
        end = NFAState()

        first_nfa = compute_regex(exp_t.left)
        second_nfa = compute_regex(exp_t.right)

        start.next_state['$'] = [first_nfa[0], second_nfa[0]]
        first_nfa[1].next_state['$'] = [end]
        second_nfa[1].next_state['$'] = [end]

        return start, end


    def do_kleene_star(exp_t):
        start = NFAState()
        end = NFAState()

        starred_nfa = compute_regex(exp_t.left)

        start.next_state['$'] = [starred_nfa[0], end]
        starred_nfa[1].next_state['$'] = [starred_nfa[0], end]

        return start, end


    def arrange_transitions(state, states_done, symbol_table):
        global nfa

        if state in states_done:
            return

        states_done.append(state)

        for symbol in list(state.next_state):
            if symbol not in nfa['letters']:
                nfa['letters'].append(symbol)
            for ns in state.next_state[symbol]:
                if ns not in symbol_table:
                    symbol_table[ns] = sorted(symbol_table.values())[-1] + 1
                    q_state = "Q" + str(symbol_table[ns])
                    nfa['states'].append(q_state)
                nfa['transition_function'].append(["Q" + str(symbol_table[state]), symbol, "Q" + str(symbol_table[ns])])

            for ns in state.next_state[symbol]:
                arrange_transitions(ns, states_done, symbol_table)

    def notation_to_num(str):
        return int(str[1:])


    def final_st_dfs():
        global nfa
        for st in nfa["states"]:
            count = 0
            for val in nfa['transition_function']:
                if val[0] == st and val[2] != st:
                    count += 1
            if count == 0 and st not in nfa["final_states"]:
                nfa["final_states"].append(st)


    def arrange_nfa(fa):
        global nfa
        nfa['states'] = []
        nfa['letters'] = []
        nfa['transition_function'] = []
        nfa['start_states'] = []
        nfa['final_states'] = []
        q_1 = "Q" + str(1)
        nfa['states'].append(q_1)
        arrange_transitions(fa[0], [], {fa[0] : 1})
        
        st_num = [notation_to_num(i) for i in nfa['states']]

        nfa["start_states"].append("Q1")
        final_st_dfs()


    def add_concat(regex):
        global non_symbols
        l = len(regex)
        res = []
        for i in range(l - 1):
            res.append(regex[i])
            if regex[i] not in non_symbols:
                if regex[i + 1] not in non_symbols or regex[i + 1] == '(':
                    res += '.'
            if regex[i] == ')' and regex[i + 1] == '(':
                res += '.'
            if regex[i] == '*' and regex[i + 1] == '(':
                res += '.'
            if regex[i] == '*' and regex[i + 1] not in non_symbols:
                res += '.'
            if regex[i] == ')' and regex[i + 1] not in non_symbols:
                res += '.'

        res += regex[l - 1]
        return res


    def compute_postfix(regexp):
        stk = []
        res = ""

        for c in regexp:
            if c not in non_symbols or c == "*":
                res += c
            elif c == ")":
                while len(stk) > 0 and stk[-1] != "(":
                    res += stk.pop()
                stk.pop()
            elif c == "(":
                stk.append(c)
            elif len(stk) == 0 or stk[-1] == "(" or compPrecedence(c, stk[-1]):
                stk.append(c)
            else:
                while len(stk) > 0 and stk[-1] != "(" and not compPrecedence(c, stk[-1]):
                    res += stk.pop()
                stk.append(c)

        while len(stk) > 0:
            res += stk.pop()

        return res

    def polish_regex(regex):
        reg = add_concat(regex)
        print(reg)
        regg = compute_postfix(reg)
        return regg
    
    def output_nfa():
        global nfa
        print(nfa)
    
    re_str = "doctor|person"
    # re_str = "a|b"
    pr = polish_regex(re_str)
    et = make_exp_tree(pr)
    fa = compute_regex(et)
    arrange_nfa(fa)

    def get_power_set(nfa_st):
        powerset = list(chain.from_iterable(combinations(nfa_st, r) for r in range(len(nfa_st)+1)))
        print("itertools: ", len(powerset))
        return powerset
    
    def out_dfa():
        global dfa
        print(dfa)
    
    dfa['states'] = []
    dfa['letters'] = nfa['letters']
    dfa['transition_function'] = []

    for state in nfa['states']:
        nfa_states.append(state)

    dfa_states = get_power_set(nfa_states)

    dfa['states'] = []
    for states in dfa_states:
        temp = []
        for state in states:
            temp.append(state)
        dfa['states'].append(temp)

    for states in dfa_states:
        for letter in nfa['letters']:
            q_to = []
            for state in states:
                for val in nfa['transition_function']:
                    start = val[0]
                    inp = val[1]
                    end = val[2]
                    if state == start and letter == inp:
                        if end not in q_to:
                            q_to.append(end)
            q_states = []
            for i in states:
                q_states.append(i)
            dfa['transition_function'].append([q_states, letter, q_to])

    dfa['start_states'] = []
    for state in nfa['start_states']:
        dfa['start_states'].append([state])
    dfa['final_states'] = []
    for states in dfa['states']:
        for state in states:
            if state in nfa['final_states'] and states not in dfa['final_states']:
                dfa['final_states'].append(states)
    
    out_dfa()
    
    # output_nfa()
