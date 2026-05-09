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
):
    try:
        import torch
        import torch.nn as nn
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import accuracy_score, classification_report
    except ImportError:
        logger.error("PyTorch and scikit-learn required. Run: pip install torch scikit-learn")
        return

    if not os.path.exists(train_path):
        logger.error(f"Training data not found: {train_path}")
        logger.error("Run `python training/prepare_dataset.py` first.")
        return

    logger.info("Loading data...")
    train_df = pd.read_csv(train_path, index_col=0)
    test_df = pd.read_csv(test_path, index_col=0)

    feature_cols = [c for c in train_df.columns if c not in ("label", "symbol")]

    # Scale features
    scaler = StandardScaler()
    X_train_raw = scaler.fit_transform(train_df[feature_cols].fillna(0))
    X_test_raw = scaler.transform(test_df[feature_cols].fillna(0))
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
    logger.info(f"\nFinal Test Accuracy: {final_acc:.4f}")
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
    args = parser.parse_args()

    train_lstm(args.train, args.test, args.model, args.scaler,
               seq_len=args.seq_len, epochs=args.epochs, batch_size=args.batch_size)


if __name__ == "__main__":
    main()
