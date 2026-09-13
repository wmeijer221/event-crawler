from typing import Optional, Tuple
import urllib.request
import asyncio
import os
from pathlib import Path
import subprocess
from urllib.error import URLError
import time
import signal
import json
import datetime
from dataclasses import asdict
from numpy import argsort
from abc import ABC, abstractmethod

from event_crawler.llm.interfaces import create_backend, HttpAgent, OllamaModelOptions, LLMResponse

DATETIME_FORMAT = "%Y%m%d_%H%M%S"
DEFAULT_MODEL = "llama3.2"


class Chatlike(ABC):
    @abstractmethod
    def chat(
        self,
        user_message: str,
        system_message: Optional[str] = None,
        options: Optional[OllamaModelOptions] = None,
        **kwargs
    ) -> LLMResponse:
        ...

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        pass


class LazyOllamaChat(Chatlike):
    """
    Wraps the Ollama interface, to start a server and everything for you,
    so you can only worry about your chat and not the other stuff that
    needs to work for you to do that.
    """

    def __init__(
        self,
        localhost: str = "http://127.0.0.1:11434",
        model: str = DEFAULT_MODEL,
        ollama_dir: Optional[str] = ".models",
        timeout_s: int = 300,
        options: Optional[OllamaModelOptions] = None,
        trace_dir: Optional[Path] = None,
        trace_file_prefix: Optional[str] = None,
        n_threads: int = 4
    ) -> None:
        self._localhost = localhost
        self._model = model
        self._ollama_dir = Path(ollama_dir).absolute().resolve() \
            if ollama_dir is not None else None
        self._timeout_s = timeout_s
        self._options = options
        self._trace_dir = trace_dir
        self._trace_file_prefix = trace_file_prefix
        self._n_threads = n_threads

        self._proc = None
        self._last_metrics = None
        self._trace_file_path = None

    def chat(self, user_message: str, system_message: Optional[str] = None, options: Optional[OllamaModelOptions] = None):
        self._user_message = user_message
        self._system_message = system_message
        options = options if options is not None else self._options
        self._backend = asyncio.run(create_backend(
            provider='ollama',
            model=self._model,
            host=self._localhost,
            timeout_s=self._timeout_s,
            options=options)
        )
        self._chat = HttpAgent(backend=self._backend, url=self._localhost)
        self._response = asyncio.run(
            self._chat.chat(user_message, system_message))
        self.create_trace()
        self._last_metrics = self._chat.cumulative_metrics
        return self._response

    def get_last_metrics(self) -> dict | None:
        return self._last_metrics

    def create_trace(self):
        if self._trace_file_path is None:
            return
        trace = {
            'model': self._model,
            'timestamp': datetime.datetime.now().strftime(DATETIME_FORMAT),
            'server_url': self._chat.url,
            'user_message': self._user_message,
            'system_message': self._system_message,
            'response': self._response,
            'max_tool_calls': self._chat.max_tool_calls,
            'cumulative_metrics': self._chat.cumulative_metrics,
            'metrics': [asdict(ele) for ele in self._chat.metrics_log],
            'messages': self._chat.messages,
            'responses': [asdict(resp) for resp in self._chat.responses]
        }
        j_trace = json.dumps(trace)
        with open(self._trace_file_path, 'a') as trace_file:
            trace_file.write(j_trace + "\n")

    def __enter__(self):
        assert self._ollama_dir is None or self._ollama_dir.exists(), "Ollama is not installed..."
        self.__create_trace_file()
        self.__start_server()
        return self

    def __start_server(self):
        if self._ollama_dir is None:
            return

        # Checks if server exists.
        try:
            urllib.request.urlopen(self._localhost)
            server_exists = True
        except URLError as ex:
            print(type(ex), str(ex))
            server_exists = False

        # Starts server if none exists.
        if not server_exists:
            print(f'Starting Ollama server myself...')
            command = [str(self._ollama_dir), 'serve']
            my_env = os.environ.copy()
            my_env["OLLAMA_NUM_PARALLEL"] = str(self._n_threads)
            self._proc = subprocess.Popen(
                command, env=my_env, start_new_session=True)
            print(f"Started Ollama server with PID: {self._proc.pid}")
            time.sleep(3)

        # Pulls right image.
        pull_command = [self._ollama_dir, 'pull', self._model]  # 15797
        _ = subprocess.run(pull_command, check=True)

    def __create_trace_file(self):
        if self._trace_dir is None:
            return
        os.makedirs(self._trace_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime(DATETIME_FORMAT)
        if self._trace_file_prefix is None:
            filename = f'trace_{timestamp}.jsonl'
        else:
            filename = f'{self._trace_file_prefix}_{timestamp}.jsonl'
        self._trace_file_path = self._trace_dir.joinpath(filename)
        with open(self._trace_file_path, 'w+') as trace_file:
            trace_file.write("")
        print(f'Created new trace file: "{str(self._trace_file_path)}"')

    def __exit__(self, exc_type, exc, tb):
        if self._proc is not None:
            print(f"Killing Ollama server with PID: {self._proc.pid}...")
            gid = os.getpgid(self._proc.pid)
            os.killpg(gid, signal.SIGKILL)


class LazyAgenticOllamaChats(Chatlike):
    def __init__(
        self,
        models_and_options: dict[str, Chatlike | Tuple[str, OllamaModelOptions]],
        localhost: str = "http://127.0.0.1:11434",
        ollama_dir: Optional[str] = ".models",
        timeout_s: int = 300,
        trace_dir: Optional[Path] = None,
        trace_file_prefix: Optional[str] = None,
    ):
        self._localhost = localhost
        self._ollama_dir = Path(ollama_dir).absolute().resolve() if ollama_dir is not None else None
        self._timeout_s = timeout_s
        self._trace_dir = trace_dir
        self._trace_file_prefix = trace_file_prefix

        self._models_and_options = models_and_options

        # We have a main chat ourselves to make hosting the server - if necessary -
        # a responsibility of this object, not any sub-object.
        self._main_chat = None
        self._agents = dict()
        self._trace_file_path = None

    def __enter__(self):
        # Using a dummy model because I don't want to download
        # anything I don't actually use.
        dummy_model_id = list(self._models_and_options.keys())[0]
        dummy_model = self._models_and_options[dummy_model_id]
        if isinstance(dummy_model, Chatlike):
            dummy_model = DEFAULT_MODEL
        else:
            dummy_model = dummy_model[0]
        self._main_chat = LazyOllamaChat(
            model=dummy_model,
            localhost=self._localhost,
            ollama_dir=self._ollama_dir,
            timeout_s=self._timeout_s,
            trace_dir=self._trace_dir,
            trace_file_prefix=self._trace_file_prefix,
        )
        self._main_chat.__enter__()
        self.__start_agents()
        return self

    def __start_agents(self):
        for agent_id, config_or_chatlike in self._models_and_options.items():
            if isinstance(config_or_chatlike, Chatlike):
                # It is a chat instance already.
                agent = config_or_chatlike
            else:
                # We need to build the chat ourselves.
                model, options = config_or_chatlike
                agent = self.__create_new_agent(agent_id, model, options)
            agent.__enter__()
            self._agents[agent_id] = agent

    def __create_new_agent(self, agent_id: str, model: str, options: OllamaModelOptions):
        trace_file_prefix = f'agent_{agent_id}' if self._trace_file_prefix is None else f'{self._trace_file_prefix}_agent_{agent_id}'
        agent = LazyOllamaChat(
            localhost=self._localhost,
            model=model,
            ollama_dir=self._ollama_dir,
            timeout_s=self._timeout_s,
            options=options,
            trace_dir=self._trace_dir,
            trace_file_prefix=trace_file_prefix
        )
        return agent

    def __exit__(self, exc_type, exc, tb):
        self._main_chat.__exit__(exc_type, exc, tb)
        self._trace_file_path = self._main_chat._trace_file_path
        self.__create_trace_file()
        for agent in self._agents.values():
            agent.__exit__(exc_type, exc, tb)

    def __create_trace_file(self):
        if self._trace_file_path is None:
            return
        traces = list()
        for agent in self._agents.values():
            agent_trace_path = agent._trace_file_path
            with open(agent_trace_path, 'r', encoding='utf-8') as agent_trace_file:
                agent_traces = agent_trace_file.readlines()
                traces.extend(agent_traces)
        parsed_traces = [json.loads(trace) for trace in traces]
        timestamp_parsed = [datetime.datetime.strptime(trace['timestamp'], DATETIME_FORMAT)
                            for trace in parsed_traces]
        sort_index = argsort(timestamp_parsed)
        sorted_traces = [traces[idx] for idx in sort_index]
        with open(self._trace_file_path, 'w+') as trace_file:
            trace_file.writelines(sorted_traces)
        for agent in self._agents.values():
            agent_trace_path = agent._trace_file_path
            os.remove(agent_trace_path)

    def chat(self, agent_id: str, user_message: str, system_message: Optional[str] = None, options: Optional[OllamaModelOptions] = None, **kwargs):
        agent_chat = self._agents[agent_id]
        response = agent_chat.chat(user_message, system_message, options, **kwargs)
        return response

    def get_cumulative_metrics(self):
        metrics = {agent_id: agent.get_last_metrics()
                   for agent_id, agent in self._agents.items()}
        return metrics


if __name__ == "__main__":
    bin_path = str(Path('.models').absolute(
    ).resolve().joinpath('bin').joinpath('ollama'))
    output_path = Path(__file__).parent.joinpath("tmp")
    output_path.mkdir(parents=True, exist_ok=True)
    llm_trace_dir = output_path.joinpath('llm_traces')
    llm_trace_dir.mkdir(parents=True, exist_ok=True)

    models_and_options = {
        'joker': ("qwen2.5-coder:32b", OllamaModelOptions(num_ctx=1000, think=False)),
        'critic': ('qwen2.5-coder:32b', OllamaModelOptions(num_ctx=1000, think=False))
    }

    with LazyAgenticOllamaChats(models_and_options=models_and_options, ollama_dir=bin_path, trace_dir=llm_trace_dir, trace_file_prefix='test') as chats:
        response = chats.chat(
            agent_id='joker', user_message='Tell me a joke. Only output the joke.', system_message='You are a funny joker.')
        response2 = chats.chat(agent_id='critic', user_message=f'The joke:\n\n"{response}"',
                               system_message='Youre a comedy critic. Rate the following joke on a scale of 1 to 10 and give a brief opinion.')
        response3 = chats.chat(
            agent_id='joker', user_message=f'The joke\n\n"{response}"\n\nThe review:\n\n"{response2}"', system_message='You are a joker. Revise your joke based on the review.')

        print("============== RESULTS ==============")
        print()
        print("Joker:")
        print(response)
        print()
        print("Critic:")
        print(response2)
        print()
        print("Joker:")
        print(response3)
