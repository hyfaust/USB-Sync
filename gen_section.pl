#!/usr/bin/env perl
use strict;
use warnings;
use utf8;

binmode STDIN,  ':encoding(UTF-8)';
binmode STDOUT, ':encoding(UTF-8)';
binmode STDERR, ':encoding(UTF-8)';

sub normalize_line {
    my ($line) = @_;
    $line =~ s/^\s+|\s+$//g;
    if (length($line) >= 2 && substr($line, 0, 1) eq '"' && substr($line, -1, 1) eq '"') {
        $line = substr($line, 1, -1);
        $line =~ s/^\s+|\s+$//g;
    }
    return $line;
}

sub is_absolute {
    my ($text) = @_;
    return $text =~ m{^(?:[A-Za-z]:[\\/]|\\\\|/)};
}

sub canon_path {
    my ($text) = @_;
    $text =~ s{/}{\\}g;
    return $text;
}

sub path_parent {
    my ($text) = @_;
    $text =~ s{/}{\\}g;
    if ($text =~ m{^(.*)[\\\/][^\\\/]+$}) {
        return $1;
    }
    return undef;
}

sub path_name {
    my ($text) = @_;
    $text =~ s{/}{\\}g;
    if ($text =~ m{([^\\\/]+)$}) {
        return $1;
    }
    return undef;
}

sub dedupe_preserve_order {
    my (@items) = @_;
    my %seen;
    my @result;
    for my $item (@items) {
        next if !defined($item) || $item eq '' || $seen{$item}++;
        push @result, $item;
    }
    return @result;
}

my @raw;
if (@ARGV) {
    my $input = shift @ARGV;
    open my $fh, '<:encoding(UTF-8)', $input or die "Cannot open $input: $!";
    @raw = <$fh>;
    close $fh;
} else {
    @raw = <STDIN>;
}

my @items;
for my $line (@raw) {
    $line = normalize_line($line);
    next if $line eq '';
    push @items, $line;
}

@items = dedupe_preserve_order(@items);

my ($base_parent, $base_name);
for my $item (@items) {
    next if !is_absolute($item);
    my $parent = path_parent($item);
    next if !defined $parent || $parent eq '';
    my $norm_parent = lc(canon_path($parent));
    if (!defined $base_parent) {
        $base_parent = canon_path($parent);
        $base_name = path_name($base_parent);
        next;
    }
    if ($norm_parent ne lc(canon_path($base_parent))) {
        die "All absolute paths must share the same parent directory.\n";
    }
}

die "No absolute path found; cannot determine the common parent directory.\n"
    if !defined $base_parent || !defined $base_name;

my @bak_names;
my %seen_name;
for my $item (@items) {
    my $name = path_name($item);
    if (is_absolute($item)) {
        my $parent = path_parent($item);
        if (!defined $parent || lc(canon_path($parent)) ne lc(canon_path($base_parent))) {
            die "All absolute paths must share the same parent directory.\n";
        }
    }
    next if !defined $name || $name eq '' || $seen_name{$name}++;
    push @bak_names, $name;
}

my $bak_list = join(',', map { '!' . $_ } @bak_names);

print "[$base_name]\n";
print "sources = $base_parent\n";
print "target = .\\target_$base_name\n";
print "ignore=*,$bak_list\n";
