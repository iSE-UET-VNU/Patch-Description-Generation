import torch
from transformers import (
    AutoModelForCausalLM, 
    CodeLlamaTokenizer,
    TrainingArguments,
    TrainerCallback,
)
from model import DualObjectiveDataCollator, DualObjectiveTrainer
from util import get_preprocessed_data
from contextlib import nullcontext
import argparse

def run(args):
    tokenizer = CodeLlamaTokenizer.from_pretrained(args.model_id)
    model = AutoModelForCausalLM.from_pretrained(args.model_id, load_in_8bit=args.load_in_8bit, device_map='auto', torch_dtype=torch.float16)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right" # Fix weird overflow issue with fp16 training
    train_dataset = get_preprocessed_data(args.dataset_id, tokenizer, 'train')

    model.train()

    def create_peft_config(model):
        from peft import (
            get_peft_model,
            LoraConfig,
            TaskType,
            prepare_model_for_int8_training,
        )

        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            inference_mode=False,
            r=8,
            lora_alpha=32,
            lora_dropout=0.05,
            target_modules = ["q_proj", "v_proj"]
        )

        # prepare int-8 model for training
        if args.load_in_8bit:
            model = prepare_model_for_int8_training(model)
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()
        return model, peft_config

    # create peft config
    model, lora_config = create_peft_config(model)

    enable_profiler = False
    # output_dir = "tmp/code-llama-output"

    config = {
        'lora_config': lora_config,
        'learning_rate': 1e-4,
        'num_train_epochs': 1,
        'gradient_accumulation_steps': 2,
        'per_device_train_batch_size': args.batch_size,
        'gradient_checkpointing': False,
    }

    # Set up profiler
    if enable_profiler:
        wait, warmup, active, repeat = 1, 1, 2, 1
        total_steps = (wait + warmup + active) * (1 + repeat)
        schedule =  torch.profiler.schedule(wait=wait, warmup=warmup, active=active, repeat=repeat)
        profiler = torch.profiler.profile(
            schedule=schedule,
            on_trace_ready=torch.profiler.tensorboard_trace_handler(f"{args.output_dir}/logs/tensorboard"),
            record_shapes=True,
            profile_memory=True,
            with_stack=True)
        
        class ProfilerCallback(TrainerCallback):
            def __init__(self, profiler):
                self.profiler = profiler
                
            def on_step_end(self, *args, **kwargs):
                self.profiler.step()

        profiler_callback = ProfilerCallback(profiler)
    else:
        profiler = nullcontext()

    # Define training args
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        overwrite_output_dir=True,
        bf16=True,  # Use BF16 if available
        # logging strategies
        logging_dir=f"{args.output_dir}/logs",
        logging_strategy="steps",
        logging_steps=10,
        save_strategy="no",
        optim="adamw_torch_fused",
        remove_unused_columns=False,
        max_steps=total_steps if enable_profiler else -1,
        **{k:v for k,v in config.items() if k != 'lora_config'}
    )

    data_collator = DualObjectiveDataCollator()

    with profiler:
        # Create Trainer instance
        trainer = DualObjectiveTrainer(
            alpha=args.alpha,
            output_rationale=False,
            model=model,
            args=training_args,
            tokenizer=tokenizer,
            train_dataset=train_dataset,
            data_collator=data_collator,
            callbacks=[profiler_callback] if enable_profiler else [],
        )

        # Start training
        trainer.train()

    model.save_pretrained(args.output_dir)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--load_in_8bit", action='store_true',
                        help="Load model 8 bit.")
    parser.add_argument("--output_dir", default='tmp', type=str,
                        help="The output directory where the model checkpoints will be written.")
    parser.add_argument("--model_id", default='codellama/CodeLlama-7b-hf', type=str,
                        help="Path to pre-trained model: e.g. codellama/CodeLlama-7b-hf")
    parser.add_argument("--dataset_id", default='zhaospei/cmg_allinone', type=str,
                        help="Path to dataset for training")
    parser.add_argument("--alpha", type=float, default=0.5)

    args = parser.parse_args()
    run(args)

if __name__ == '__main__':
    main()