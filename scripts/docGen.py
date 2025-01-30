from transformers import logging, AutoTokenizer, TextStreamer, AutoModelForCausalLM
import os
import time
from datetime import datetime
import argparse
import torch

# export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

logging.set_verbosity_error()

# parse arguments
parser = argparse.ArgumentParser(description="Generates military documents via an LLM based on user input")
parser.add_argument("-t", "--max-tokens", type=int, help="Specify the max number of tokens when generating the document, default is 2000", default=2000)
args = parser.parse_args()

# Set LLM instructions
ROLE = "Role: You work for the United States Department of the Navy, and you specialize in writing official military documents using military formatting.\n"
SYSTEM = "Give your answer in naval message format based on the previous examples. After the final line of the document you create, stop responding and give an eos token."

def gen(model_num: int, type_num: int, prompt: str, save: bool = True) -> str:
    """Generates a specificed document using a specified LLM and returns the result.

    :param model_num: The number value representing the model to use for generation.
    :type model_num: int
    :param type_num: The number value representing the type of document to generate.
    :type type_num: int
    :param prompt: The prompting given to the LLM to use for generation.
    :type prompt: str
    :param save: Specifies if the output should be saved to the disk or not, defaults to True
    :type save: bool, optional
    :return: The document produced by the LLM
    :rtype: str
    """
    # create model objects
    model_name = select_model(model_num)
    doc_type = select_doc(type_num)
    model = AutoModelForCausalLM.from_pretrained(model_name, device_map="auto", torch_dtype=torch.float16)
    tokenizer = AutoTokenizer.from_pretrained(model_name, padding_size="left")
    tokenizer.pad_token = tokenizer.eos_token
    streamer = TextStreamer(tokenizer, skip_prompt=True)
    
    # set up prompt info
    examples = load_examples(doc_type)
    prompt = ROLE + examples + prompt + SYSTEM
    
    # set device based on gpu availability
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_inputs = tokenizer([prompt], return_tensors="pt", padding=True).to(device)
    
    # generate response
    t_start = time.time()
    # TODO: Is the streamer actually working as intended, too much overhead? How does this relate to batch_decode?
    generated_ids = model.generate(**model_inputs, do_sample=True, max_new_tokens=args.max_tokens, streamer=streamer)
    # TODO: Do I need to slice off the prompt?
    response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0][len(prompt):]
    t_stop = time.time()
    print(f"Generation time: {t_stop - t_start} sec / {(t_stop - t_start) / 60} min")
    
    if save:
        save_response(response, prompt, model_name, model)
    
    return response
    
def load_examples(type: str) -> str:
    """Returns real life examples of the requested document type.

    :param type: The document type
    :type type: str
    :return: A string of all of the examples concatenated together
    :rtype: str
    """
    # load in examples for few shot prompting
    examples = "Read the following examples very carefully. Your response must follow the same formatting as these examples.\n"

    data_path = "../data"
    files = []

    # get paths to all example files
    for root, dirs, fnames in os.walk(data_path):
        for f in fnames:
            if type in f:
                files.append(os.path.join(root, f))

    # append contents of each file to examples
    for f in files:
        with open(f, 'r') as file:
            examples += f"Example:\n {file.read()}\n\n"
            
    return examples

def save_response(response: str, prompt: str, model_name: str, model: str):
    """Writes the response and information about how it was generated to the disk.

    :param response: The document generated by the LLM
    :type response: str
    :param prompt: The prompt given by the user to the LLM to generate the document
    :type prompt: str
    :param model_name: The LLM model used to generate the document
    :type model_name: str
    :param model: The model object
    :type model: str
    """    
    # save response to file
    fname = datetime.now().strftime("%d-%b-%Y_%H:%M:%S")
    path = f"../output/{fname}.txt"
    with open(path, 'w') as file:
        file.write(f"---------- RESPONSE ----------\n{response}\n\n")
        file.write(f"---------- PROMPT ----------\n{prompt}\n\n")
        file.write(f"---------- MODEL ----------\n{model_name}\n{model}")

def select_model(num: int) -> str:
    """Translates between the numeric representation of a model to the full string of its name.

    :param num: The model number selected from the menu
    :type num: int
    :return: The full string of the model name to fetch from the Hugging Face hub
    :rtype: str
    """    
    model = ""
    
    match num:
        case 1:
            model = "meta-llama/Llama-3.2-1B-Instruct"
        case 2:
            model = "meta-llama/Llama-3.2-3B-Instruct"
        case 3:
            model = "meta-llama/Llama-3.1-8B-Instruct"
        case 4:
            model = "meta-llama/Llama-3.3-70B-Instruct"

    return model

def select_doc(num: int) -> str:
    """Translates between the numeric representation of a document type and its full name.

    :param num: The document number selected from the menu
    :type num: int
    :return: The full string of the document type to generate and fetch examples
    :rtype: str
    """
    type = ""
    
    match num:
        case 1:
            type = "NAVADMIN"
        case 2:
            type = "MARADMIN"
        case 3:
            type = "OpOrd"
        case _:
            type = "NAVADMIN"

    return type

if __name__ == "__main__":
    # model to select model you want to load
    select = int(input("Select the Llama model you would like to run\n1) Llama 3.2 1B Instruct\n2) Llama 3.2 3B Instruct\n3) Llama 3.1 8B Instruct\n4) Llama 3.3 70B Instruct\n> "))

    # get document type from user
    doc = int(input("Select document to generate\n1) Naval Message (NAVADMIN)\n2) USMC Message (MARADMIN)\n3) USMC OpOrd\n> "))

    user_query = input("Input> ")
    user_query = "Request: " + user_query

    # call document generator function
    doc = gen(select, doc, user_query)