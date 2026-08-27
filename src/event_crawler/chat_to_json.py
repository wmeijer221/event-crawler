from ollama import Client
from ollama import ChatResponse
import json
import subprocess
import time
import regex as re
import httpx
import os


class ChatToJson:
    def __init__(self, llm_model: str, n_threads: int = 1, timeout: int = 1200, max_retries_on_exception: int = 2):
        self._model = llm_model
        self._n_threads = n_threads
        self._max_retries_on_exception = max_retries_on_exception
        self._client = Client(timeout=timeout)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.end()

    def start(self) -> None:
        command = ['ollama', 'serve']
        my_env = os.environ.copy()
        my_env["OLLAMA_NUM_PARALLEL"] = str(self._n_threads)
        self._proc = subprocess.Popen(command, env=my_env)
        time.sleep(2)

    def end(self) -> None:
        self._client.close()
        self._proc.kill()
        self._proc.wait()

    def handle(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
        assistant_prompt: str | None = None,
        options: dict | None = None,
        output_model: dict | None = None
    ) -> dict[str, str] | list[dict[str, str]]:
        if output_model is None:
            output_model = dict()
        messages = list()
        if system_prompt is not None:
            messages.append({
                'role': 'system',
                'content': system_prompt
            })
        if assistant_prompt is not None:
            messages.append({
                'role': 'assistant',
                'content': assistant_prompt
            })
        messages.append({
            'role': 'user',
            'content': user_prompt,
        })

        if options is None:
            options = {
                "top_p": 0.9,
                "num_predict": 10000
            }

        for _ in range(self._max_retries_on_exception):
            data_ = list()
            try:
                response: ChatResponse = self._client.chat(
                    model=self._model,
                    messages=messages,
                    options=options
                )
                j_data = response.message.content
                pattern = r'\\u(?![0-9a-fA-F]{4})'
                cleaned_json = re.sub(pattern, r'\\\\u', j_data)
                data = json.loads(cleaned_json)
                # Sometimes it's silly and outputs an array of arrays...
                # So we flatten the results.
                for ele in data:
                    if isinstance(ele, list):
                        data_.extend(ele)
                    else:
                        data_.append(ele)
                for ele in data_:
                    if not isinstance(ele, dict):
                        raise ValueError(
                            f"Expected a dictionary but got {type(ele)}: {ele}")
                    for key, value in output_model.items():
                        if key not in ele:
                            ele[key] = value
                return data_
            except (json.JSONDecodeError, httpx.ReadTimeout, ValueError) as ex:
                print(ex)
                continue
        return list()
