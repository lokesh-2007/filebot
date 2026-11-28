"""Machine learning assignment 3: k-nearest neighbours classifier from scratch."""
import math

def distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

def knn_predict(train, labels, point, k=3):
    ranked = sorted(zip(train, labels), key=lambda pair: distance(pair[0], point))
    top = [label for _, label in ranked[:k]]
    return max(set(top), key=top.count)
