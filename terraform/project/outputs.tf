output "vector_bucket_name" {
  value = aws_s3vectors_vector_bucket.chunks.vector_bucket_name
}

output "vector_indexes" {
  description = "BUCKET/INDEX for each live version, as mitos-eval --vector-index takes it."
  value       = [for index in aws_s3vectors_index.chunks : "${index.vector_bucket_name}/${index.index_name}"]
}
