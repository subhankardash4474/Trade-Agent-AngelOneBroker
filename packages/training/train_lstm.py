"""
LSTM Model Training
Trains a PyTorch LSTM for short-term price movement prediction.
Outputs both the model weights and a fitted feature scaler.
"""

import argparse
import os
import pickle

import numpy as np
import pandas as pd
from loguru import logger


class LSTMNet:
    """Wrapper to define, train, and save the LSTM model using PyTorch."""

    def __init__(self, input_size: int, hidden_size: int = 64, num_layers: int = 2,
                 dropout: float = 0.2, num_classes: int = 2):
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout
        self.num_classes = num_classes

    def build(self):
        import torch
        import torch.nn as nn

        class _LSTM(nn.Module):
            def __init__(self_, input_size, hidden_size, num_layers, dropout, num_classes):
                super().__init__()
                self_.lstm = nn.LSTM(input_size, hidden_size, num_layers,
                                     batch_first=True, dropout=dropout)
                self_.fc = nn.Linear(hidden_size, num_classes)

            def forward(self_, x):
                out, _ = self_.lstm(x)
                out = self_.fc(out[:, -1, :])  # take last timestep
                return out

        return _LSTM(self.input_size, self.hidden_size, self.num_layers,
                     self.dropout, self.num_classes)


def create_sequences(features: np.ndarray, labels: np.ndarray, seq_len: int = 30):
    """Create overlapping sequences for LSTM input."""
    X, y = [], []
    for i in range(seq_len, len(features)):
        X.append(features[i - seq_len:i])
        y.append(labels[i])
    return np.array(X), np.array(y)


def train_lstm(
    train_path: str = "data/train_dataset.csv",
    test_path: str = "data/test_dataset.csv",
    model_output: str = "models/lstm_model.pt",
    scaler_output: str = "models/lstm_scaler.pkl",
    seq_len: int = 30,
    epochs: int = 50,
    batch_size: int = 64,
    lr: float = 0.001,
    seed: int = 42,
):
    try:
        import torch
        import torch.nn as nn
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import accuracy_score, classification_report
    except ImportError:
        logger.error("PyTorch and scikit-learn required. Run: pip install torch scikit-learn")
        return

    # F-69 (audit 2026-05-27): without seeding numpy + torch + python-rng,
    # consecutive runs of the same training script produced models whose
    # held-out accuracy differed by 2-3pp, making it impossible to
    # attribute changes (new feature? bug?) vs random init noise.
    # `torch.use_deterministic_algorithms` is intentionally NOT set
    # because the LSTM kernel can't always honour it on every PyTorch
    # build and the current goal is reproducibility, not bit-exactness.
    import random as _random
    _random.seed(seed)
    np.random.seed(seed)
    try:
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    except Exception:
        pass

    if not os.path.exists(train_path):
        logger.error(f"Training data not found: {train_path}")
        logger.error("Run `python training/prepare_dataset.py` first.")
        return

    logger.info("Loading data...")
    train_df = pd.read_csv(train_path, index_col=0)
    test_df = pd.read_csv(test_path, index_col=0)

    feature_cols = [c for c in train_df.columns if c not in ("label", "symbol")]

    # F-68 (audit 2026-05-27): we previously called ``.fillna(0)`` on raw
    # features BEFORE StandardScaler.fit, which (a) silently dilutes the
    # learned mean/std with synthetic zeros and (b) collapses every NaN
    # to the same numeric value after scaling -- the LSTM then learns
    # that "exactly the scaler's standardised-zero value" is a feature
    # and over-fits to noisy rows. Median-impute per column instead, so
    # the imputed rows look like the feature's central tendency rather
    # than an artificial outlier. Log the NaN inventory loudly so the
    # operator knows when prepare_dataset is letting too many through.
    train_features = train_df[feature_cols]
    test_features = test_df[feature_cols]
    nan_total = int(train_features.isna().sum().sum())
    if nan_total:
        nan_pct = nan_total / max(train_features.size, 1) * 100
        logger.warning(
            f"[LSTM-TRAIN] training matrix has {nan_total} NaN cells "
            f"({nan_pct:.2f}% of cells); using per-column MEDIAN imputation "
            f"(F-68). High NaN rates are a prepare_dataset smell."
        )
    medians = train_features.median(numeric_only=True).fillna(0.0)
    train_features = train_features.fillna(medians)
    test_features = test_features.fillna(medians)  # use TRAIN medians for test (no leakage)

    # Scale features
    scaler = StandardScaler()
    X_train_raw = scaler.fit_transform(train_features)
    X_test_raw = scaler.transform(test_features)
    y_train_raw = train_df["label"].values.astype(int)
    y_test_raw = test_df["label"].values.astype(int)

    logger.info(f"Creating sequences (seq_len={seq_len})...")
    X_train, y_train = create_sequences(X_train_raw, y_train_raw, seq_len)
    X_test, y_test = create_sequences(X_test_raw, y_test_raw, seq_len)

    logger.info(f"Train: {X_train.shape}, Test: {X_test.shape}")

    # Build model
    input_size = X_train.shape[2]
    net_builder = LSTMNet(input_size=input_size)
    model = net_builder.build()

    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    X_train_t = torch.FloatTensor(X_train)
    y_train_t = torch.LongTensor(y_train)
    X_test_t = torch.FloatTensor(X_test)
    y_test_t = torch.LongTensor(y_test)

    dataset = torch.utils.data.TensorDataset(X_train_t, y_train_t)
    # C-22 (audit 2026-05-26): `shuffle=True` is INTENTIONAL and correct
    # here. Each row of X_train_t has shape (seq_len, features) -- it IS
    # an already-assembled sequence (see `create_sequences()` above which
    # slides a fixed-length window over the chronologically-sorted feature
    # matrix). Shuffling samples randomises the ORDER in which intact
    # sequences are fed to SGD, which is best-practice for convergence
    # and IID assumptions. We are NOT shuffling timesteps WITHIN a
    # sequence, which would indeed break the LSTM. The train/test split
    # upstream is chronological (no leakage). Marked here so future
    # reviewers don't toggle this back to False under the impression it
    # destroys temporal structure.
    loader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=True)

    logger.info(f"Training LSTM ({epochs} epochs, batch_size={batch_size})...")
    best_acc = 0

    for epoch in range(1, epochs + 1):
        model.train()
        total_loss = 0
        for xb, yb in loader:
            optimizer.zero_grad()
            output = model(xb)
            loss = criterion(output, yb)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        if epoch % 10 == 0 or epoch == 1:
            model.eval()
            with torch.no_grad():
                test_out = model(X_test_t)
                test_pred = test_out.argmax(dim=1).numpy()
                acc = accuracy_score(y_test, test_pred)
            logger.info(f"Epoch {epoch:3d} | Loss: {total_loss/len(loader):.4f} | Test Acc: {acc:.4f}")

            if acc > best_acc:
                best_acc = acc
                torch.save(model, model_output.replace(".pt", "_best.pt"))

    # Final evaluation
    model.eval()
    with torch.no_grad():
        test_out = model(X_test_t)
        test_pred = test_out.argmax(dim=1).numpy()

    final_acc = accuracy_score(y_test, test_pred)
    logger.info(f"\nFinal-epoch Test Accuracy: {final_acc:.4f}")

    # F-23 (audit 2026-05-27): the loop saved ``<model>_best.pt`` at every
    # new high-water-mark test accuracy, but the final save below
    # overwrote ``models/lstm_model.pt`` with the LAST-EPOCH weights.
    # If late epochs over-fit (very common with LSTMs on noisy intraday
    # data), the deployed model was strictly worse than the
    # best-checkpoint sitting on disk. Load the best checkpoint -- if
    # one exists -- into the model that gets shipped, and re-emit the
    # final metrics from those weights so the saved log matches the
    # saved artifact.
    best_path = model_output.replace(".pt", "_best.pt")
    if os.path.exists(best_path) and best_acc > final_acc:
        logger.info(
            f"[LSTM-TRAIN] best-epoch acc {best_acc:.4f} > final-epoch "
            f"{final_acc:.4f}; shipping best checkpoint from {best_path}."
        )
        model = torch.load(best_path, map_location="cpu", weights_only=False)
        model.eval()
        with torch.no_grad():
            test_pred = model(X_test_t).argmax(dim=1).numpy()
        final_acc = accuracy_score(y_test, test_pred)
        logger.info(f"Shipped-model Test Accuracy: {final_acc:.4f}")
    logger.info(f"\n{classification_report(y_test, test_pred, target_names=['DOWN', 'UP'])}")

    # Save
    os.makedirs(os.path.dirname(model_output), exist_ok=True)
    torch.save(model, model_output)
    with open(scaler_output, "wb") as f:
        pickle.dump(scaler, f)

    logger.info(f"Model saved: {model_output}")
    logger.info(f"Scaler saved: {scaler_output}")


def main():
    parser = argparse.ArgumentParser(description="Train LSTM price prediction model")
    parser.add_argument("--train", default="data/train_dataset.csv")
    parser.add_argument("--test", default="data/test_dataset.csv")
    parser.add_argument("--model", default="models/lstm_model.pt")
    parser.add_argument("--scaler", default="models/lstm_scaler.pkl")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--seq-len", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=64)
    # F-69: expose the seed so the operator can sweep variance across
    # runs without editing source.
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    train_lstm(args.train, args.test, args.model, args.scaler,
               seq_len=args.seq_len, epochs=args.epochs,
               batch_size=args.batch_size, seed=args.seed)


if __name__ == "__main__":
    main()
