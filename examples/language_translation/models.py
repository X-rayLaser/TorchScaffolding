from torch import nn
from torch.nn import functional as F


class Encoder(nn.Module):
    def __init__(self, input_size, hidden_size):
        super().__init__()
        self.hidden_size = hidden_size
        self.embedding = nn.Embedding(input_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size, batch_first=True)

    def forward(self, x, hidden):
        embedded = self.embedding(x)
        output, hidden = self.gru(embedded, hidden)
        return output, hidden

    def run_inference(self, x, hidden):
        return self.forward(x, hidden)


class Decoder(nn.Module):
    def __init__(self, hidden_size, output_size):
        super().__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size

        self.embedding = nn.Embedding(output_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size, batch_first=True)
        self.out = nn.Linear(hidden_size, output_size)

    def forward(self, x, hidden):
        output = self.embedding(x)

        output = F.relu(output)

        output, hidden = self.gru(output, hidden)
        output = self.out(output)
        return output, hidden

    def run_inference(self, x, hidden):
        # here is will mean SOS character
        import torch
        outputs = []
        for i in range(10):
            x, hidden = self.forward(x, hidden)
            top = torch.argmax(x, dim=2).squeeze()
            x = torch.LongTensor([[top]])
            outputs.append(top.item())

        return [outputs]
