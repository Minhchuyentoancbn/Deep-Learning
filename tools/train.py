import torch
import torch.nn as nn
import torch.optim as optim
import pytorch_lightning as pl

from timm.optim import create_optimizer_v2
from tools.utils import Averager
from dataloaders import create_dataset, create_dataloader
from models import Model


class LitModel(pl.LightningModule):
    def __init__(self, model: Model, args=None):
        super().__init__()

        self.model = model
        self.args = args
        self.criterion = nn.BCELoss()

        # Metrics
        self.loss_val_avg = Averager()
        self.acc_val_avg = Averager()
        self.f1_val_avg = Averager()
        self.precision_val_avg = Averager()
        self.recall_val_avg = Averager()

        self.automatic_optimization = False

        self.best_f1 = 0.0

    def configure_optimizers(self):
        feat_optimizer = create_optimizer_v2(
            self.model.feature_extractor,
            "AdamW", lr=self.args.lr, weight_decay=self.args.weight_decay
        )
        head_optimizer = create_optimizer_v2(
            self.model.classifier,
            "AdamW", lr=self.args.lr_head, weight_decay=self.args.weight_decay
        )

        feat_scheduler = optim.lr_scheduler.OneCycleLR(
            feat_optimizer, max_lr=self.args.lr,
            total_steps=self.args.epochs,
            pct_start=self.args.warmup_pct, cycle_momentum=False,
        )
        head_scheduler = optim.lr_scheduler.OneCycleLR(
            head_optimizer, max_lr=self.args.lr_head,
            total_steps=self.args.epochs,
            pct_start=self.args.warmup_pct, cycle_momentum=False,
        )

        return [feat_optimizer, head_optimizer], [feat_scheduler, head_scheduler]
    
    def training_step(self, batch, batch_idx):
        # Get the optimizer
        feat_optimizer, head_optimizer = self.optimizers()

        # Get the data
        images, labels, _ = batch
        # batch_size = images.size(0)

        # Forward pass
        self.model.train()
        preds = self.model(images)
        loss = self.criterion(preds, labels)

        # Backward pass
        feat_optimizer.zero_grad()
        head_optimizer.zero_grad()
        self.manual_backward(loss)
        self.clip_gradients(feat_optimizer, self.args.clip_grad, "norm")
        self.clip_gradients(head_optimizer, self.args.clip_grad, "norm")
        feat_optimizer.step()
        head_optimizer.step()

        # Log training loss
        self.log('loss', loss.item(), reduce_fx="mean", prog_bar=True, on_step=False, on_epoch=True)
        self.log("feat_lr", feat_optimizer.param_groups[0]["lr"], prog_bar=True, on_step=False, on_epoch=True)
        self.log("head_lr", head_optimizer.param_groups[0]["lr"], prog_bar=True, on_step=False, on_epoch=True)
        

    def on_train_epoch_end(self):
        # Get the scheduler
        feat_scheduler, head_scheduler = self.lr_schedulers()
        feat_scheduler.step()
        head_scheduler.step()

    def validation_step(self, batch, batch_idx):
        # Get the data
        images, labels, _ = batch
        batch_size = images.size(0)

        # Forward pass
        self.model.eval()
        with torch.no_grad():
            preds = self.model(images)
        loss = self.criterion(preds, labels)

        # Calculate metrics
        preds = (preds > 0.5).float()
        acc = (preds == labels).sum() / (batch_size * 10)
        tp = (preds * labels).sum()
        fp = (preds * (1 - labels)).sum()
        fn = ((1 - preds) * labels).sum()
        precision = tp / (tp + fp)
        recall = tp / (tp + fn)
        f1 = 2 * precision * recall / (precision + recall)

        # Log validation loss
        self.log('val_loss', loss.item(), reduce_fx="mean", prog_bar=True, on_step=False, on_epoch=True)
        self.log('val_acc', acc.item(), reduce_fx="mean", prog_bar=True, on_step=False, on_epoch=True)
        self.log('val_f1', f1.item(), reduce_fx="mean", prog_bar=True, on_step=False, on_epoch=True)
        self.log('val_precision', precision.item(), reduce_fx="mean", prog_bar=True, on_step=False, on_epoch=True)
        self.log('val_recall', recall.item(), reduce_fx="mean", prog_bar=True, on_step=False, on_epoch=True)

        # Update metrics
        self.loss_val_avg.add(loss.item())
        self.acc_val_avg.add(acc.item())
        self.f1_val_avg.add(f1.item())
        self.precision_val_avg.add(precision.item())
        self.recall_val_avg.add(recall.item())


    def on_validation_epoch_end(self):
        current_f1 = self.f1_val_avg.val()
        if current_f1 > self.best_f1:
            self.best_f1 = current_f1
            torch.save(self.model.state_dict(), f"saved_models/{self.args.model}_val.pt")

        print('-' * 10)
        print(f"Validation loss: {self.loss_val_avg.val():.4f}")
        print(f"Validation accuracy: {self.acc_val_avg.val():.4f}")
        print(f"Validation f1: {self.f1_val_avg.val():.4f}")
        print(f"Validation precision: {self.precision_val_avg.val():.4f}")
        print(f"Validation recall: {self.recall_val_avg.val():.4f}")
        self.loss_val_avg.reset()
        self.acc_val_avg.reset()
        self.f1_val_avg.reset()
        self.precision_val_avg.reset()
        self.recall_val_avg.reset()


def train(args):

    # Create dataset and dataloader
    if args.train_full:
        train_dataset = create_dataset(args.train_dirs + args.val_dirs, is_train=True)
        train_loader = create_dataloader(
            train_dataset, batch_size=args.batch_size, 
            num_workers=args.num_workers ,is_train=True
        )
    else:
        train_dataset = create_dataset(args.train_dirs, is_train=True)
        val_dataset = create_dataset(args.val_dirs, is_train=False)

        train_loader = create_dataloader(
            train_dataset, batch_size=args.batch_size, 
            num_workers=args.num_workers ,is_train=True
        )
        val_loader = create_dataloader(
            val_dataset, batch_size=args.batch_size, 
            num_workers=args.num_workers ,is_train=True
        )

    ori_model = Model(args.model, 10, args.mlp_structures, args.drop_rate)
    pl_model = LitModel(ori_model, args)

    trainer = pl.Trainer(
        default_root_dir=f"logs/{args.model}/",
        max_epochs=args.epochs,
    )

    print("Start training")
    print('-' * 25)

    if args.train_full:
        trainer.fit(pl_model, train_loader)
        torch.save(pl_model.model.state_dict(), f"saved_models/{args.model}_full.pt")
        return pl_model.model
    else:
        trainer.fit(pl_model, train_loader, val_loader)

        print(f"Best validation f1: {pl_model.best_f1:.4f}")

        model = pl_model.model
        model.load_state_dict(torch.load(f"saved_models/{args.model}_val.pt"))
        return model
    